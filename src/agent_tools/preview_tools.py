"""preview_file -- let an engineering agent SEE a file it built.

Why this exists (2026-10-01): a Lead Engineer worker drew the "Agamemnon helmet"
logo as an inline SVG path in static/index.html. It was a headphones-and-
microphone shape. The contract test passed because it checked strings, the
orchestrator called the work done, and the user saw the wrong logo in the UI; a
later reviewer caught it only by reading path coordinates. Penpot designers can
look at their work (`render_preview`) and a read-only critic has a browser, but
nothing could render a file from a repository worktree. This tool does, with the
same path policy `read_file` uses, and hands the model a PNG.

It is read-only: it renders in a throwaway headless Chromium profile whose only
reachable network origin is a tiny confined HTTP server (see :class:`_PreviewServer`),
and writes nothing into the workspace.

Why not ``file://`` (2026-10-01, hardening before first use): a page opened from
``file://`` can ``<iframe src="file:///app/data/...">`` or ``<img src=file:///...>``
any file the browser process can read, so a hostile repository (agents clone
third-party code) could put secrets into a screenshot that ``read_file`` would
have refused. ``EXCLUDE localhost`` in the resolver rules also let the page reach
every service on the container's loopback, and ``/static/app.js`` (root-relative)
does not resolve under ``file://`` so real app pages rendered unstyled. Serving
over HTTP from a confined server fixes all three: an http origin cannot load
``file://``, the browser sends everything but that one origin to a dead proxy,
and ``/`` is the workspace root.

Returning the image: the result carries ``images`` in the same shape MCP tools
produce (``[{"data": <base64>, "mimeType": ...}]``). The loop already forwards
``images[0]`` to the UI. Getting it to the MODEL is a separate hop -- see
:func:`model_image_followup` and the note on it.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import io
import json
import logging
import mimetypes
import os
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 800
# Same ceiling for both axes: a tall full-page-style request is a way to make the
# model pay for a huge image, not a way to see more of the page.
MAX_VIEWPORT = 2560
MIN_VIEWPORT = 16
MAX_SCALE = 4
# An icon asked for with no size would otherwise be a 1280x800 page with a dot in
# the corner; SVG is drawn to fill the viewport, so it gets a square default.
DEFAULT_SVG_SIZE = 512
# Source files above these are refused: a page or SVG that large is not a design
# under review, and a multi-hundred-MB "image" is a decompression bomb.
MAX_MARKUP_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 25 * 1024 * 1024
# What the model is sent. Providers downscale anything bigger anyway; resizing
# here keeps the base64 (and the replayed context) small.
MAX_RETURN_SIDE = 2048
MAX_RETURN_BYTES = 8 * 1024 * 1024
# Chromium's virtual-time budget: lets scripts, fonts and images settle without
# waiting wall-clock time. The subprocess timeout sits well above it.
SETTLE_MS = 3000
RENDER_TIMEOUT_S = 45
# Per-file ceiling for what the preview server hands the browser (the page
# itself is already capped by MAX_MARKUP_BYTES; this covers its assets).
MAX_SERVED_BYTES = 25 * 1024 * 1024
# Chromium sends everything except the preview origin to this closed port; a
# connect to port 9 (discard) is refused at once on any sane host.
DEAD_PROXY = "http://127.0.0.1:9"
WRAPPER_PATH = "/__odysseus_preview__/wrapper.html"

_HTML_EXTS = {".html", ".htm", ".xhtml"}
_SVG_EXTS = {".svg"}
_IMAGE_MIMES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_BROWSER_HINT = (
    "No Chrome/Chromium found to render with. Install one or set "
    "ODYSSEUS_BROWSER_EXECUTABLE to its full path."
)


def find_browser() -> str:
    """The browser to render with, or ''.

    ``builtin_mcp._find_browser_executable`` knows the env override and Linux
    locations; ``penpot_text.browser_executable`` wraps it and adds the Windows
    and macOS Chrome/Edge installs, which is what a dev box has.
    """
    try:
        from src.penpot_text import browser_executable

        return browser_executable() or ""
    except Exception:  # noqa: BLE001 - discovery must never break the tool
        try:
            from src.builtin_mcp import _find_browser_executable

            return _find_browser_executable() or ""
        except Exception:  # noqa: BLE001
            return ""


def _clamp(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def build_chromium_argv(
    exe: str,
    url: str,
    out_png: str,
    profile_dir: str,
    *,
    width: int,
    height: int,
    scale: int = 1,
    color_scheme: str = "light",
    settle_ms: int = SETTLE_MS,
    server_port: Optional[int] = None,
) -> List[str]:
    """The headless Chromium command line (flags verified on Chrome, 2026-10-01).

    Dark mode: ``--blink-settings=preferredColorScheme=`` is what makes
    ``prefers-color-scheme`` match deterministically (0 = dark, 1 = light on the
    new headless). ``--force-dark-mode`` alone is no use: headless follows the
    OS theme by default, so a dark-themed dev box rendered every "light" page
    dark, and combined with the blink setting the blink setting wins anyway.

    Network: ``MAP * ~NOTFOUND`` makes every hostname fail to resolve so a page
    cannot phone home. Hostnames are not the only way out: an IP literal
    (``http://127.0.0.1:8080``) skips the resolver, and Chrome bypasses proxies
    for loopback by default. So all traffic goes to a dead proxy, and
    ``<-loopback>`` in the bypass list REMOVES that implicit loopback bypass; the
    one bypass left is the preview server's own ``127.0.0.1:<port>``. Any other
    local port is sent to a closed port and fails (verified 2026-10-01).
    """
    bypass = "<-loopback>" + (f";127.0.0.1:{server_port}" if server_port else "")
    dark = str(color_scheme or "").strip().lower() == "dark"
    return [
        exe,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--hide-scrollbars",
        "--disable-extensions",
        "--no-first-run",
        "--mute-audio",
        f"--force-device-scale-factor={scale}",
        f"--window-size={width},{height}",
        f"--user-data-dir={profile_dir}",
        f"--virtual-time-budget={settle_ms}",
        f"--blink-settings=preferredColorScheme={0 if dark else 1}",
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
        f"--proxy-server={DEAD_PROXY}",
        f"--proxy-bypass-list={bypass}",
        f"--screenshot={out_png}",
        url,
    ]


def _server_root_for(path: str) -> str:
    """The directory the preview server is rooted at for an already-resolved file.

    The workspace when the file is inside it, else the managed worktree of the
    workspace's repository that holds it (the same two roots
    ``_resolve_tool_path`` accepts), so ``/static/app.js`` means what it means in
    the repository. A file reachable some other way (a vault note, an
    attachment) is rooted at its own directory: nothing else is exposed.
    """
    from src.tool_execution import _path_within, get_active_workspace

    workspace = get_active_workspace()
    if workspace:
        try:
            base = os.path.realpath(workspace)
            if _path_within(path, base):
                return base
            from src.agent_worktree.ownership import workspace_worktree_for

            top = workspace_worktree_for(path, workspace)
            if top:
                return os.path.realpath(top)
        except Exception:  # noqa: BLE001 - fall back to the narrowest root
            pass
    return os.path.dirname(path)


_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".xhtml": "application/xhtml+xml", ".svg": "image/svg+xml",
    ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json",
    ".wasm": "application/wasm", ".woff2": "font/woff2", ".woff": "font/woff",
    ".ttf": "font/ttf", ".otf": "font/otf", ".txt": "text/plain; charset=utf-8",
}


def _content_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in _CONTENT_TYPES:
        return _CONTENT_TYPES[ext]
    # Anything unrecognised is inert bytes (and nosniff is set), never something
    # the browser may guess into script.
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


class _PreviewServer:
    """A short-lived HTTP server confined to one directory tree, for ONE render.

    Every request is resolved through ``_resolve_tool_path`` (read semantics, the
    chat's private-vault grant honoured) AND must realpath inside ``root``, so a
    page cannot read anything ``read_file`` would refuse, nor anything outside the
    tree through ``..``, an encoded slash, or a symlink. GET/HEAD only, no
    directory listings; every refusal is the same bare 404 so it does not
    confirm what exists. It binds 127.0.0.1 on an ephemeral port; the browser is
    locked to that origin in :func:`build_chromium_argv`.
    """

    def __init__(self, root: str, allow_private: bool = False, wrapper_html: str = ""):
        self.root = os.path.realpath(root)
        self.wrapper = wrapper_html.encode("utf-8")
        self.allow_private = allow_private
        # The workspace and attachment lineage are context variables; the server
        # threads do not inherit them, so each request runs in a copy of the
        # context that was current when the render started.
        self._context = contextvars.copy_context()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1] if self._httpd else 0

    def url_for(self, path: str) -> str:
        rel = os.path.relpath(path, self.root).replace(os.sep, "/")
        return "/" + quote(rel, safe="/")

    @property
    def wrapper_url(self) -> str:
        return f"http://127.0.0.1:{self.port}{WRAPPER_PATH}"

    def resolve(self, request_path: str) -> Optional[str]:
        """The file to serve for a request path, or None (404)."""
        from src.tool_execution import _path_within, _resolve_tool_path

        raw = unquote(urlsplit(request_path).path)
        if "\x00" in raw or "\\" in raw or not raw.startswith("/"):
            return None
        parts = [p for p in raw.split("/") if p not in ("", ".")]
        if not parts or any(p == ".." for p in parts):
            return None
        candidate = os.path.realpath(os.path.join(self.root, *parts))
        if not _path_within(candidate, self.root):
            return None
        try:
            resolved = self._context.copy().run(
                _resolve_tool_path, candidate,
                allow_private=self.allow_private, allow_attachment_read=True,
            )
        except (ValueError, TypeError, OSError):
            return None
        resolved = os.path.realpath(resolved)
        if not _path_within(resolved, self.root) or not os.path.isfile(resolved):
            return None
        try:
            if os.path.getsize(resolved) > MAX_SERVED_BYTES:
                return None
        except OSError:
            return None
        return resolved

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):  # silence stderr noise
                return

            def _send(self, status, ctype="text/plain", body=b"", head=False):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if not head:
                    self.wfile.write(body)

            def _serve(self, head):
                try:
                    if urlsplit(self.path).path == WRAPPER_PATH:
                        return self._send(200, "text/html; charset=utf-8", outer.wrapper, head)
                    target = outer.resolve(self.path)
                    if target is None:
                        return self._send(404, head=head)
                    with open(target, "rb") as handle:
                        body = handle.read(MAX_SERVED_BYTES + 1)
                    if len(body) > MAX_SERVED_BYTES:
                        return self._send(404, head=head)
                    return self._send(200, _content_type(target), body, head)
                except Exception:  # noqa: BLE001 - never leak a traceback to the page
                    try:
                        self._send(404, head=head)
                    except Exception:  # noqa: BLE001
                        pass

            def do_GET(self):  # noqa: N802
                self._serve(False)

            def do_HEAD(self):  # noqa: N802
                self._serve(True)

            def _refuse(self):
                self.send_response(405)
                self.send_header("Allow", "GET, HEAD")
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse  # noqa: N815

        return Handler

    def start(self) -> "_PreviewServer":
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        httpd.daemon_threads = True
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
            name="odysseus-preview-server", daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
            finally:
                httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


# Headless Chromium on Windows sizes the OUTER window: asked for 600x600 it laid
# the page out at 584x449 (frame and toolbar take 16px and ~150px) and would not
# go narrower than ~500px, so "375px wide" was never a phone viewport and an SVG
# shot at 128px came out as a corner of a 500px page (verified 2026-10-01).
# So the page is never rendered in the window directly. It is shown inside a
# wrapper whose <iframe>/<img> is EXACTLY the requested size (an iframe is its
# own viewport: media queries and 100vh follow it), the window is made bigger
# than that, and the shot is cropped back to the requested box.
_WINDOW_PAD_W = 64
_WINDOW_PAD_H = 300
_MIN_WINDOW_W = 520


def _outer_window(width: int, height: int) -> Tuple[int, int]:
    return max(width + _WINDOW_PAD_W, _MIN_WINDOW_W), height + _WINDOW_PAD_H


def _frame_html(target_url: str, width: int, height: int, dark: bool, is_svg: bool) -> str:
    """Wrapper page showing the target in a width x height box at the origin.

    An SVG goes in an <img>: it fills the box (opened directly it renders at its
    intrinsic size, a 24px icon in a big shot), and an <img> cannot run script
    or fetch anything. The background is opaque: a transparent PNG shows as black
    in some viewers and hides a dark logo on a dark page.
    """
    background = "#16181d" if dark else "#ffffff"
    box = f"width:{width}px;height:{height}px;border:0;display:block"
    if is_svg:
        inner = f'<img src="{target_url}" alt="" style="{box};object-fit:contain">'
    else:
        inner = f'<iframe src="{target_url}" style="{box};background:{background}"></iframe>'
    return (
        "<!doctype html><meta charset=utf-8>"
        f"<style>html,body{{margin:0;overflow:hidden;background:{background}}}</style>{inner}"
    )


def _crop_to_viewport(png: bytes, width: int, height: int, scale: int) -> bytes:
    from PIL import Image

    image = Image.open(io.BytesIO(png))
    image.load()
    box = (0, 0, min(width * scale, image.width), min(height * scale, image.height))
    out = io.BytesIO()
    image.crop(box).save(out, format="PNG")
    return out.getvalue()


def _fit_image(data: bytes, mime: str) -> Tuple[bytes, str, Tuple[int, int]]:
    """Return image bytes the model can be sent, resizing only when needed.

    PNG/JPEG/WebP that are already small go through byte-for-byte. A GIF (first
    frame), an oversized image, or anything whose bytes exceed the cap is
    decoded and re-encoded as PNG. Raises ValueError for undecodable data.
    """
    try:
        from PIL import Image
    except ImportError:  # no Pillow: pass through what is safe to pass
        if mime == "image/gif":
            raise ValueError("cannot convert a GIF without Pillow; try PNG, JPEG or WebP")
        if len(data) > MAX_RETURN_BYTES:
            raise ValueError("image is too large to return and Pillow is not installed to shrink it")
        return data, mime, (0, 0)
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:  # noqa: BLE001 - PIL raises many types
        raise ValueError(f"not a readable image: {exc}") from exc
    size = image.size
    needs_resize = max(size) > MAX_RETURN_SIDE
    if mime != "image/gif" and not needs_resize and len(data) <= MAX_RETURN_BYTES:
        return data, mime, size
    if needs_resize:
        image.thumbnail((MAX_RETURN_SIDE, MAX_RETURN_SIDE))
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA")
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue(), "image/png", image.size


def _error(message: str) -> Dict[str, Any]:
    return {"error": f"preview_file: {message}", "exit_code": 1}


def _parse_args(content: str) -> Dict[str, Any]:
    stripped = (content or "").strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return {"path": stripped.split("\n", 1)[0].strip()}


async def render_with_chromium(
    exe: str,
    url: str,
    *,
    width: int,
    height: int,
    scale: int,
    color_scheme: str,
    profile_dir: str,
    out_png: str,
    server_port: Optional[int] = None,
) -> Optional[str]:
    """Run the screenshot; return an error string, or None on success."""
    argv = build_chromium_argv(
        exe, url, out_png, profile_dir,
        width=width, height=height, scale=scale, color_scheme=color_scheme,
        server_port=server_port,
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
    except OSError as exc:
        return f"could not start the browser ({exe}): {exc}"
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=RENDER_TIMEOUT_S)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return f"rendering timed out after {RENDER_TIMEOUT_S}s (the page may never finish loading)"
    if not os.path.isfile(out_png) or os.path.getsize(out_png) < 100:
        tail = (err or b"").decode("utf-8", "replace")[-300:].strip()
        return "the browser produced no screenshot" + (f": {tail}" if tail else "")
    return None


class PreviewFileTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.private_access import context_allows_private
        from src.tool_execution import _resolve_tool_path

        args = _parse_args(content)
        raw_path = str(args.get("path") or "").strip()
        if not raw_path:
            return _error("path is required")
        color_scheme = str(args.get("color_scheme") or "light").strip().lower()
        if color_scheme not in ("light", "dark"):
            return _error("color_scheme must be 'light' or 'dark'")

        # The SAME resolver read_file uses, so this never renders what read_file
        # would refuse (outside the workspace, .ssh, app state, hard links).
        try:
            try:
                path = _resolve_tool_path(raw_path, allow_private=context_allows_private(ctx),
                                          allow_attachment_read=True)
            except TypeError:
                path = _resolve_tool_path(raw_path)
        except ValueError as exc:
            return _error(str(exc))

        if os.path.isdir(path):
            return _error(f"{path} is a directory; pass a file (use ls to find one)")
        if not os.path.isfile(path):
            return _error(f"{path}: not found")
        ext = os.path.splitext(path)[1].lower()
        is_markup = ext in _HTML_EXTS or ext in _SVG_EXTS
        if not is_markup and ext not in _IMAGE_MIMES:
            return _error(
                f"unsupported file type '{ext or '(none)'}'; use .html, .svg, .png, .jpg, .webp or .gif"
            )
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            return _error(f"{path}: {exc}")
        limit = MAX_MARKUP_BYTES if is_markup else MAX_IMAGE_BYTES
        if size > limit:
            return _error(f"{path} is {size} bytes, over the {limit} byte limit for previews")

        if not is_markup:
            return await self._passthrough(path, ext)

        width = _clamp(args.get("width"), DEFAULT_SVG_SIZE if ext in _SVG_EXTS else DEFAULT_WIDTH,
                       MIN_VIEWPORT, MAX_VIEWPORT)
        height = _clamp(args.get("height"), DEFAULT_SVG_SIZE if ext in _SVG_EXTS else DEFAULT_HEIGHT,
                        MIN_VIEWPORT, MAX_VIEWPORT)
        scale = _clamp(args.get("scale"), 1, 1, MAX_SCALE)

        exe = find_browser()
        if not exe:
            return _error(_BROWSER_HINT)

        tmp = tempfile.mkdtemp(prefix="odysseus-preview-")
        server: Optional[_PreviewServer] = None
        try:
            out_png = os.path.join(tmp, "preview.png")
            # The port is only known after bind, so the wrapper (which names the
            # target by a root-relative URL) is built after the server starts.
            server = _PreviewServer(_server_root_for(path), allow_private=context_allows_private(ctx))
            server.start()
            server.wrapper = _frame_html(
                server.url_for(path), width, height, color_scheme == "dark", ext in _SVG_EXTS
            ).encode("utf-8")
            win_w, win_h = _outer_window(width, height)
            failure = await render_with_chromium(
                exe, server.wrapper_url, width=win_w, height=win_h, scale=scale,
                color_scheme=color_scheme, profile_dir=os.path.join(tmp, "profile"), out_png=out_png,
                server_port=server.port,
            )
            if failure:
                return _error(failure)
            with open(out_png, "rb") as handle:
                raw = handle.read()
        finally:
            if server is not None:
                server.stop()
            shutil.rmtree(tmp, ignore_errors=True)

        try:
            raw = await asyncio.to_thread(_crop_to_viewport, raw, width, height, scale)
            data, mime, (w, h) = await asyncio.to_thread(_fit_image, raw, "image/png")
        except (ValueError, OSError, ImportError) as exc:
            return _error(f"could not process the screenshot: {exc}")
        return _result(path, data, mime, f"{width}x{height} viewport, {color_scheme}, {scale}x", w, h)

    async def _passthrough(self, path: str, ext: str) -> dict:
        def _load() -> bytes:
            with open(path, "rb") as handle:
                return handle.read()

        try:
            raw = await asyncio.to_thread(_load)
            data, mime, (w, h) = await asyncio.to_thread(_fit_image, raw, _IMAGE_MIMES[ext])
        except ValueError as exc:
            return _error(f"{path}: {exc}")
        except OSError as exc:
            return _error(f"{path}: {exc}")
        return _result(path, data, mime, "image file, no browser needed", w, h)


def _result(path: str, data: bytes, mime: str, how: str, w: int, h: int) -> dict:
    dims = f" {w}x{h}px," if w and h else ""
    return {
        "output": (
            f"Rendered {path} ({how};{dims} {len(data)} bytes). The image is attached: "
            "look at it and compare it with the reference or the request."
        ),
        "exit_code": 0,
        "images": [{"data": base64.b64encode(data).decode("ascii"), "mimeType": mime}],
    }


# The marker agent_loop.HARNESS_USER_SOURCES skips: a follow-up carrying tool
# images is the harness talking, never the person's request.
TOOL_IMAGES_SOURCE = "tool_images"
TOOL_IMAGES_PREFIX = "[Tool images —"
# Per round. A tool that returns a gallery should not make one round cost ten
# images of context; the rest are named in the note so the model can ask again.
MAX_FOLLOWUP_IMAGES = 3
# Base64 characters per image (~4.5 MB of bytes). Providers reject or downscale
# anything bigger, and it would sit in every later request until pruned.
MAX_FOLLOWUP_IMAGE_CHARS = 6_000_000


def _image_data_url(image: Any) -> Optional[str]:
    if not isinstance(image, dict):
        return None
    data = image.get("data")
    if not isinstance(data, str) or not data:
        return None
    if data.startswith("data:"):
        return data
    mime = str(image.get("mimeType") or image.get("mime_type") or "image/png")
    if not mime.startswith("image/"):
        mime = "image/png"
    return f"data:{mime};base64,{data}"


def model_image_followup(
    records: List[dict],
    *,
    accept_images: bool = True,
    max_images: int = MAX_FOLLOWUP_IMAGES,
    max_chars: int = MAX_FOLLOWUP_IMAGE_CHARS,
) -> Optional[dict]:
    """A user-role message that carries this round's tool images to the model.

    Tool messages are text-only on every route (OpenAI chat ``tool`` content,
    Responses ``function_call_output.output`` as built by
    ``chatgpt_subscription.build_responses_input``), so an ``images`` list on a
    tool result reached the UI but never the model (2026-10-01: a designer
    agent told to "never claim visual verification without viewing a render"
    could not view one, and an engineer shipped a headphones glyph as a helmet).
    The portable way, and what Codex CLI does for ``view_image``, is a follow-up
    user message with the image directly after the tool messages. Pass this
    round's ``tool_result_records`` (each with a ``result`` dict, optionally a
    ``call_id``); returns None when no result has an image.

    The message is marked ``metadata.source = tool_images`` and untrusted, so
    person-request detection, delegation gates and hand-back logic skip it, and
    it is never persisted (the loop's in-turn messages are not saved to chat
    history). ``accept_images=False`` (a model without vision) swaps the images
    for a text note saying so, rather than sending what the route would reject
    or silently drop.
    """
    found: List[tuple] = []  # (label, data_url or None when over the size cap)
    for record in records or []:
        result = record.get("result") if isinstance(record, dict) else None
        images = result.get("images") if isinstance(result, dict) else None
        if not isinstance(images, list):
            continue
        name = str(record.get("tool_name") or "tool")
        call_id = record.get("call_id")
        label = f"{name} (call {call_id})" if call_id else name
        for image in images:
            url = _image_data_url(image)
            if url is None:
                continue
            found.append((label, url if len(url) <= max_chars else None))
    if not found:
        return None

    meta = {"trusted": False, "source": TOOL_IMAGES_SOURCE, "tool_gate_untrusted": False}
    labels = ", ".join(dict.fromkeys(label for label, _ in found))
    if not accept_images:
        text = (
            f"{TOOL_IMAGES_PREFIX} {len(found)} image(s) from {labels} could not be shown: "
            "this model does not accept images. Do not claim to have seen them; rely on the "
            "tool's text output, or say you could not view the image.]"
        )
        return {"role": "user", "content": [{"type": "text", "text": text}], "metadata": meta}

    shown = [(label, url) for label, url in found if url is not None][:max_images]
    omitted = len(found) - len(shown)
    lines = [f"{i}. {label}" for i, (label, _) in enumerate(shown, 1)]
    note = (
        f"{TOOL_IMAGES_PREFIX} the image(s) below are what the tool call(s) above returned, in "
        "this order. Harness-generated, not a message from the user.\n" + "\n".join(lines)
    )
    if omitted:
        note += (
            f"\n({omitted} more image(s) not shown: over the per-round limit of {max_images} "
            "or too large; call the tool again for fewer or smaller ones.)"
        )
    if not shown:
        return {"role": "user", "content": [{"type": "text", "text": note + "]"}], "metadata": meta}
    parts = [{"type": "image_url", "image_url": {"url": url}} for _, url in shown]
    return {"role": "user", "content": [{"type": "text", "text": note + "]"}] + parts, "metadata": meta}
