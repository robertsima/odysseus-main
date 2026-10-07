"""Penpot design operations the stock Penpot MCP tools cannot do.

Why this exists (2026-09-30 diagnostics, Penpot Product Designer): the stdio
Penpot MCP offers rectangles, circles, text and frames, each written to the
page's ROOT frame. A designer agent therefore could not nest anything in a
board (layouts overlapped), could not place a real logo (it stacked circles),
could not see what it had made (no render), and its text boxes carried
``length * 0.6 * size`` bounds that never matched the drawn text.

This module talks to Penpot's HTTP API with the same access token the Penpot
MCP server uses and adds:

* a declarative builder: one call writes a whole nested tree of boards, shapes,
  text and vector icons, positions relative to the parent;
* SVG import (:mod:`src.penpot_svg`) and an icon-library search (Iconify:
  200k+ open-licensed icons, including game-icons' helmets and swords);
* a layout linter that reads a page back and reports overflow and overlap;
* a real render of a board through Penpot's own viewer.

Network and file changes live here; the MCP server in
``mcp_servers/penpot_studio_server.py`` is a thin wrapper.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import tempfile
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from src import penpot_svg, penpot_text

logger = logging.getLogger(__name__)

ROOT_ID = "00000000-0000-0000-0000-000000000000"
ICONIFY_API = os.environ.get("ODYSSEUS_ICONIFY_API", "https://api.iconify.design").rstrip("/")
MAX_NODES = 600
MAX_DEPTH = 12


class PenpotError(RuntimeError):
    """A failure whose message tells the agent what to do next."""


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

@dataclass
class PenpotConfig:
    base_url: str
    token: str
    source: str
    public_base: str = ""

    @property
    def public_url(self) -> str:
        # PENPOT_PUBLIC_URL stays a fallback after the penpot_public_url setting
        # (load_config fills public_base from the setting).
        return (self.public_base or os.environ.get("PENPOT_PUBLIC_URL", "")).strip().rstrip("/") or self.base_url


def _clean_base(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url.endswith("/api"):
        url = url[:-4]
    return url


def _saved_penpot_env() -> Optional[Tuple[Dict[str, str], str]]:
    """The env of the Penpot MCP server saved under Settings > MCP, if any.

    The Penpot designer already has a working URL and access token there; the
    studio tools reuse them so nothing is configured twice.
    """
    try:
        from core.database import McpServer, SessionLocal
    except Exception:  # pragma: no cover - only when the app DB is unavailable
        return None
    db = SessionLocal()
    try:
        rows = db.query(McpServer).filter(McpServer.is_enabled == True).all()  # noqa: E712
        for row in rows:
            haystack = " ".join(str(v or "") for v in (row.name, row.command, row.args)).lower()
            if "penpot" not in haystack:
                continue
            try:
                env = json.loads(row.env) if row.env else {}
            except ValueError:
                continue
            if isinstance(env, dict) and env.get("PENPOT_ACCESS_TOKEN") and (
                    env.get("PENPOT_API_URL") or env.get("PENPOT_BASE_URL")):
                return {k: str(v) for k, v in env.items()}, f"MCP server {row.name!r}"
    except Exception:  # pragma: no cover
        return None
    finally:
        db.close()
    return None


_borrowed_logged = False


def _settings_config() -> Tuple[str, str, str]:
    """(url, token, public url) from the Penpot settings; blanks when unreadable."""
    try:
        from src.settings import get_setting

        return (str(get_setting("penpot_api_url", "") or "").strip(),
                str(get_setting("penpot_access_token", "") or "").strip(),
                str(get_setting("penpot_public_url", "") or "").strip())
    except Exception:  # pragma: no cover - settings unimportable at boot
        return "", "", ""


def load_config() -> PenpotConfig:
    """Penpot URL and token: settings first, then environment, then the borrowed
    MCP row.

    Borrowing the credentials of a saved MCP server whose name contains "penpot"
    is deprecated (2026-10-01: an integration owns its credentials). It still
    works because the owner's server relies on it; it logs once so the move to
    the settings is visible.
    """
    global _borrowed_logged
    s_url, s_token, s_public = _settings_config()
    url, token, source = s_url, s_token, "settings"
    if not (url and token):
        url = os.environ.get("PENPOT_API_URL") or os.environ.get("PENPOT_BASE_URL") or s_url
        token = os.environ.get("PENPOT_ACCESS_TOKEN") or s_token
        source = "environment"
    if not (url and token):
        saved = _saved_penpot_env()
        if saved:
            env, source = saved
            url = env.get("PENPOT_API_URL") or env.get("PENPOT_BASE_URL") or ""
            token = env.get("PENPOT_ACCESS_TOKEN") or ""
            if url and token and not _borrowed_logged:
                _borrowed_logged = True
                logger.info(
                    "Penpot Studio is using the URL and token of %s. That fallback is deprecated; "
                    "set penpot_api_url and penpot_access_token in Settings to stop depending on it.", source)
    if not (url and token):
        raise PenpotError(
            "Penpot is not configured for the studio tools. Set the Penpot URL and access token "
            "under Settings > Penpot, or set PENPOT_API_URL and PENPOT_ACCESS_TOKEN in the "
            "container environment.")
    return PenpotConfig(_clean_base(url), token, source, public_base=_clean_base(s_public))


class PenpotClient:
    """Minimal async client for Penpot's ``/api/rpc/command`` interface."""

    def __init__(self, cfg: PenpotConfig, timeout: float = 45.0):
        self.cfg = cfg
        self._http = httpx.AsyncClient(timeout=timeout, headers={
            "Authorization": f"Token {cfg.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    async def aclose(self) -> None:
        await self._http.aclose()

    async def rpc(self, command: str, body: Optional[dict] = None) -> Any:
        url = f"{self.cfg.base_url}/api/rpc/command/{command}"
        try:
            resp = await self._http.post(url, content=json.dumps(body or {}))
        except httpx.HTTPError as exc:
            raise PenpotError(
                f"cannot reach Penpot at {self.cfg.base_url} ({type(exc).__name__}: {exc}). "
                "Inside the Agamemnon container this must be the host's LAN address, not localhost.") from exc
        if resp.status_code == 401:
            raise PenpotError("Penpot rejected the access token (401). Make a new one under Penpot > "
                              "Your account > Access tokens and update the Penpot MCP server.")
        if resp.status_code >= 400:
            try:
                err = resp.json()
            except ValueError:
                err = resp.text[:400]
            raise PenpotError(f"Penpot {command} failed ({resp.status_code}): {json.dumps(err)[:700]}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    async def get_file(self, file_id: str) -> dict:
        return await self.rpc("get-file", {"id": file_id})

    async def apply(self, file_id: str, changes: List[dict], retries: int = 3) -> dict:
        """update-file with the current revision, retrying if someone else saved."""
        last: Optional[PenpotError] = None
        for _ in range(max(1, retries)):
            file = await self.get_file(file_id)
            try:
                return await self.rpc("update-file", {
                    "id": file_id,
                    "session-id": str(uuid.uuid4()),
                    "revn": file.get("revn", 0),
                    "vern": file.get("vern", 0),
                    "changes": changes,
                })
            except PenpotError as exc:
                last = exc
                if "revn" not in str(exc).lower() and "conflict" not in str(exc).lower():
                    raise
                await asyncio.sleep(0.4)
        raise last or PenpotError("update-file failed")


# ---------------------------------------------------------------------------
# Shape construction (Penpot JSON uses kebab-case keys on the wire)
# ---------------------------------------------------------------------------

def _rect_geom(x: float, y: float, w: float, h: float) -> dict:
    return {
        "x": x, "y": y, "width": w, "height": h, "rotation": 0,
        "selrect": {"x": x, "y": y, "width": w, "height": h,
                    "x1": x, "y1": y, "x2": x + w, "y2": y + h},
        "points": [{"x": x, "y": y}, {"x": x + w, "y": y},
                   {"x": x + w, "y": y + h}, {"x": x, "y": y + h}],
        "transform": {"a": 1.0, "b": 0.0, "c": 0.0, "d": 1.0, "e": 0.0, "f": 0.0},
        "transform-inverse": {"a": 1.0, "b": 0.0, "c": 0.0, "d": 1.0, "e": 0.0, "f": 0.0},
    }


_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def norm_color(value: Any, default: Optional[str] = None) -> Optional[str]:
    """``#abc`` / ``#aabbcc`` / ``None`` -> ``#aabbcc`` (Penpot wants 6 digits)."""
    if value in (None, "", "none", "transparent"):
        return default
    text = str(value).strip()
    if not _HEX.match(text):
        raise PenpotError(f"colour {value!r} is not a #rgb or #rrggbb hex value")
    if len(text) == 4:
        text = "#" + "".join(ch * 2 for ch in text[1:])
    return text.lower()


def _fills(color: Any, opacity: float = 1.0) -> List[dict]:
    c = norm_color(color)
    return [{"fill-color": c, "fill-opacity": opacity}] if c else []


def _strokes(spec: Any, default_align: str = "inner") -> List[dict]:
    if not spec:
        return []
    if isinstance(spec, str):
        spec = {"color": spec}
    color = norm_color(spec.get("color"), "#000000")
    return [{
        "stroke-color": color,
        "stroke-opacity": float(spec.get("opacity", 1)),
        "stroke-width": float(spec.get("width", 2)),
        "stroke-style": spec.get("style", "solid"),
        "stroke-alignment": spec.get("align", default_align),
    }]


def _shadows(spec: Any) -> List[dict]:
    if not spec:
        return []
    items = spec if isinstance(spec, list) else [spec]
    out = []
    for item in items:
        out.append({
            "id": str(uuid.uuid4()),
            "style": item.get("style", "drop-shadow"),
            "offset-x": float(item.get("x", 6)),
            "offset-y": float(item.get("y", 6)),
            "blur": float(item.get("blur", 0)),
            "spread": float(item.get("spread", 0)),
            "hidden": False,
            "color": {"color": norm_color(item.get("color"), "#000000"),
                      "opacity": float(item.get("opacity", 1))},
        })
    return out


def _num(node: dict, *names: str, default: Optional[float] = None) -> Optional[float]:
    for name in names:
        if node.get(name) is not None:
            try:
                return float(node[name])
            except (TypeError, ValueError):
                raise PenpotError(f"{name!r} must be a number, got {node[name]!r}")
    return default


FONT_SLUG = re.compile(r"[^a-z0-9]+")


def font_fields(family: str, weight: str, italic: bool) -> dict:
    """Penpot's Google-font identifiers: ``gfont-work-sans`` + variant ``700``."""
    weight = str(weight)
    if weight in ("normal", "regular"):
        weight = "400"
    elif weight == "bold":
        weight = "700"
    if weight == "400":
        variant = "italic" if italic else "regular"
    else:
        variant = f"{weight}italic" if italic else weight
    return {
        "font-id": "gfont-" + FONT_SLUG.sub("-", family.lower()).strip("-"),
        "font-family": family,
        "font-variant-id": variant,
        "font-weight": weight,
        "font-style": "italic" if italic else "normal",
    }


def text_spec(node: dict) -> penpot_text.TextSpec:
    """The measurable description of a text node (also used to build it)."""
    text = str(node.get("text", ""))
    if not text:
        raise PenpotError("text node has no 'text'")
    size = _num(node, "size", "font_size", default=16) or 16
    shown = text.upper() if node.get("uppercase") else text
    weight = str(node.get("weight", "400"))
    weight = {"normal": "400", "regular": "400", "bold": "700"}.get(weight, weight)
    return penpot_text.TextSpec(
        text=shown, family=str(node.get("family", "Work Sans")), weight=weight,
        italic=bool(node.get("italic")), size=float(size),
        letter_spacing=float(node.get("letter_spacing", 0) or 0),
        max_width=_num(node, "w", "width"))


def _collect_text_nodes(nodes: Iterable[Any], out: List[dict], depth: int = 0) -> None:
    for node in nodes or []:
        if not isinstance(node, dict) or depth > MAX_DEPTH:
            continue
        if str(node.get("type", "")).lower() == "text":
            out.append(node)
        _collect_text_nodes(node.get("children"), out, depth + 1)


_COMMON_KEYS = {"type", "name", "x", "y", "opacity", "shadow", "blur"}
_BOX = {"w", "h", "width", "height"}
_SHAPES = ("frame", "board", "rect", "rectangle", "ellipse", "circle")
_ALLOWED_KEYS = {
    "frame": _COMMON_KEYS | _BOX | {"fill", "fill_opacity", "radius", "stroke", "clip", "children"},
    "rect": _COMMON_KEYS | _BOX | {"fill", "fill_opacity", "radius", "stroke"},
    "ellipse": _COMMON_KEYS | _BOX | {"size", "fill", "fill_opacity", "stroke"},
    "text": _COMMON_KEYS | {"text", "size", "weight", "family", "color", "align", "valign", "w", "width",
                            "line_height", "letter_spacing", "uppercase", "italic"},
    "icon": _COMMON_KEYS | _BOX | {"icon", "svg", "url", "size", "color", "stroke_color", "recolor"},
    "path": _COMMON_KEYS | _BOX | {"d", "viewbox", "color", "stroke_color", "recolor"},
    "group": _COMMON_KEYS | {"children"},
}
_ALLOWED_KEYS["board"] = _ALLOWED_KEYS["frame"]
_ALLOWED_KEYS["rectangle"] = _ALLOWED_KEYS["rect"]
_ALLOWED_KEYS["circle"] = _ALLOWED_KEYS["ellipse"]
_ALLOWED_KEYS["svg"] = _ALLOWED_KEYS["icon"]

_ALIASES = {
    "font_size": "size", "fontsize": "size", "font_family": "family", "font": "family", "fontfamily": "family",
    "font_weight": "weight", "fontweight": "weight", "text_align": "align", "textalign": "align",
    "fill_color": "fill", "background": "fill", "text_color": "color", "font_color": "color",
    "border_radius": "radius", "corner_radius": "radius", "lineheight": "line_height",
    "letterspacing": "letter_spacing", "content": "text", "view_box": "viewbox", "icon_name": "icon",
    "stroke_colour": "stroke_color", "colour": "color",
}
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def normalize_nodes(nodes: Any, ignored: List[str], depth: int = 0) -> Any:
    """Accept the spellings a model reaches for (fontSize, font_family, ...) and
    report the fields that mean nothing, instead of silently dropping them.

    On 2026-09-30 a designer's text came out as Work Sans 16px because its
    style fields were not ones the builder reads, and nothing said so.
    """
    if not isinstance(nodes, list):
        return nodes
    out = []
    for raw in nodes:
        if not isinstance(raw, dict) or depth > MAX_DEPTH:
            out.append(raw)
            continue
        node: Dict[str, Any] = {}
        for key, value in raw.items():
            k = _CAMEL.sub("_", str(key)).lower()
            node[_ALIASES.get(k, k)] = value
        kind = str(node.get("type", "")).lower()
        if node.pop("font_style", None) == "italic":
            node["italic"] = True
        if node.pop("text_transform", None) == "uppercase":
            node["uppercase"] = True
        if kind in ("icon", "svg", "path") and "fill" in node and "color" not in node:
            node["color"] = node.pop("fill")
        if kind in _SHAPES:
            width = node.pop("stroke_width", None)
            scolor = node.pop("stroke_color", None)
            if "stroke" not in node and (scolor or width):
                node["stroke"] = {"color": scolor or "#000000", "width": width or 2}
        allowed = _ALLOWED_KEYS.get(kind)
        if allowed:
            extra = sorted(k for k in node if k not in allowed)
            if extra:
                label = node.get("name") or node.get("text") or ""
                ignored.append(f"{kind} {label!r}: ignored unknown field(s) {', '.join(extra)}".replace(" ''", ""))
        if isinstance(node.get("children"), list):
            node["children"] = normalize_nodes(node["children"], ignored, depth + 1)
        out.append(node)
    return out


class _Builder:
    """Turns a declarative node tree into ``add-obj`` changes."""

    def __init__(self, page_id: str, parent: dict, origin: Tuple[float, float],
                 measured: Optional[Dict[int, penpot_text.Measured]] = None):
        self.measured = measured or {}
        self.inexact_text = 0
        self.page_id = page_id
        self.changes: List[dict] = []
        self.created: List[dict] = []
        self.origin = origin
        self.parent = parent  # {"id", "frame", "is_frame"}
        self.count = 0
        self.attributions: List[str] = []
        self.notes: List[str] = []

    # -- helpers ----------------------------------------------------------
    def _add(self, obj: dict, parent: dict, frame_id: str) -> None:
        obj["parent-id"] = parent["id"]
        obj["frame-id"] = frame_id
        self.changes.append({
            "type": "add-obj", "id": obj["id"], "page-id": self.page_id,
            "parent-id": parent["id"], "frame-id": frame_id, "obj": obj,
        })
        self.created.append({
            "id": obj["id"], "name": obj.get("name"), "type": obj["type"],
            "x": round(obj["x"], 1), "y": round(obj["y"], 1),
            "width": round(obj["width"], 1), "height": round(obj["height"], 1),
            "parent": parent["id"],
        })

    def _common(self, node: dict, kind: str, default_name: str, x: float, y: float,
                w: float, h: float) -> dict:
        obj = {"id": str(uuid.uuid4()), "type": kind, "name": str(node.get("name") or default_name),
               **_rect_geom(x, y, w, h)}
        if node.get("opacity") is not None:
            obj["opacity"] = float(node["opacity"])
        shadow = _shadows(node.get("shadow"))
        if shadow:
            obj["shadow"] = shadow
        if node.get("blur"):
            obj["blur"] = {"id": str(uuid.uuid4()), "type": "layer-blur",
                           "value": float(node["blur"]), "hidden": False}
        return obj

    # -- node types -------------------------------------------------------
    async def build(self, node: Any, parent: dict, ox: float, oy: float, depth: int = 0) -> Optional[dict]:
        if not isinstance(node, dict):
            raise PenpotError("each node must be an object with a 'type'")
        if depth > MAX_DEPTH:
            raise PenpotError(f"nesting deeper than {MAX_DEPTH} levels")
        self.count += 1
        if self.count > MAX_NODES:
            raise PenpotError(f"more than {MAX_NODES} nodes in one build; split it into several calls")
        kind = str(node.get("type", "")).lower()
        x = ox + (_num(node, "x", default=0) or 0)
        y = oy + (_num(node, "y", default=0) or 0)
        frame_id = parent["id"] if parent["is_frame"] else parent["frame"]
        handler = {
            "frame": self._frame, "board": self._frame, "rect": self._rect, "rectangle": self._rect,
            "ellipse": self._ellipse, "circle": self._ellipse, "text": self._text,
            "icon": self._svg, "svg": self._svg, "path": self._path, "group": self._group,
        }.get(kind)
        if handler is None:
            raise PenpotError(f"unknown node type {kind!r}; use frame, rect, ellipse, text, icon, svg, path or group")
        return await handler(node, parent, frame_id, x, y, depth)

    async def _frame(self, node, parent, frame_id, x, y, depth):
        w = _num(node, "w", "width")
        h = _num(node, "h", "height")
        if not w or not h:
            raise PenpotError(f"frame {node.get('name')!r} needs w and h")
        obj = self._common(node, "frame", "Board", x, y, w, h)
        obj.update({
            "fills": _fills(node.get("fill", "#ffffff"), float(node.get("fill_opacity", 1))),
            "strokes": _strokes(node.get("stroke")),
            "shapes": [], "show-content": True, "hide-in-viewer": False,
            "clip-content": bool(node.get("clip", True)),
        })
        r = _num(node, "radius", default=0) or 0
        obj.update({"r1": r, "r2": r, "r3": r, "r4": r})
        is_board = parent["id"] == ROOT_ID
        if is_board:
            frame_id = ROOT_ID
        self._add(obj, parent, frame_id)
        me = {"id": obj["id"], "frame": obj["id"], "is_frame": True}
        for child in node.get("children") or []:
            await self.build(child, me, x, y, depth + 1)
        return obj

    async def _rect(self, node, parent, frame_id, x, y, depth):
        w = _num(node, "w", "width")
        h = _num(node, "h", "height")
        if not w or not h:
            raise PenpotError(f"rect {node.get('name')!r} needs w and h")
        obj = self._common(node, "rect", "Rectangle", x, y, w, h)
        obj["fills"] = _fills(node.get("fill"), float(node.get("fill_opacity", 1)))
        obj["strokes"] = _strokes(node.get("stroke"))
        r = _num(node, "radius", default=0) or 0
        obj.update({"r1": r, "r2": r, "r3": r, "r4": r})
        self._add(obj, parent, frame_id)
        return obj

    async def _ellipse(self, node, parent, frame_id, x, y, depth):
        w = _num(node, "w", "width", "size")
        h = _num(node, "h", "height", "size")
        if not w or not h:
            raise PenpotError(f"ellipse {node.get('name')!r} needs w and h (or size)")
        obj = self._common(node, "circle", "Ellipse", x, y, w, h)
        obj["fills"] = _fills(node.get("fill"), float(node.get("fill_opacity", 1)))
        obj["strokes"] = _strokes(node.get("stroke"))
        self._add(obj, parent, frame_id)
        return obj

    async def _text(self, node, parent, frame_id, x, y, depth):
        spec = text_spec(node)
        size = spec.size
        lh = _num(node, "line_height", default=1.2) or 1.2
        m = self.measured.get(id(node)) or penpot_text.estimate(spec)
        if not m.exact:
            self.inexact_text += 1
        line_px = size * lh
        fixed_w = spec.max_width
        box_w = fixed_w or max(line.width for line in m.lines) or size
        box_h = len(m.lines) * line_px
        color = norm_color(node.get("color"), "#000000")
        fonts = font_fields(spec.family, spec.weight, spec.italic)
        transform = "uppercase" if node.get("uppercase") else "none"
        align = str(node.get("align", "left"))
        size_s = str(int(size) if size == int(size) else size)
        style = {
            **fonts, "font-size": size_s, "text-decoration": "none", "text-transform": transform,
            "letter-spacing": str(spec.letter_spacing), "line-height": str(lh),
            "fills": [{"fill-color": color, "fill-opacity": 1}],
        }
        paragraphs = []
        for line in str(node.get("text", "")).split("\n"):
            paragraphs.append({"type": "paragraph", "text-align": align, **style,
                               "children": [{**style, "text": line}]})
        # one record per DRAWN line; y is the bottom of the line box
        position = []
        for i, line in enumerate(m.lines):
            lx = x + {"center": (box_w - line.width) / 2, "right": box_w - line.width}.get(align, 0.0)
            position.append({
                "x": round(lx, 2), "y": round(y + (i + 1) * line_px, 2),
                "width": round(line.width, 2), "height": round(line_px, 2),
                "text": line.text, **style, "direction": "ltr",
            })
        obj = self._common(node, "text", str(node.get("name") or str(node["text"]).split("\n")[0][:40]),
                           x, y, box_w, box_h)
        obj.update({
            "content": {"type": "root", "children": [{"type": "paragraph-set", "children": paragraphs}]},
            "grow-type": "auto-height" if fixed_w else "auto-width",
            "vertical-align": str(node.get("valign", "top")),
            "position-data": position, "fills": [],
        })
        self._add(obj, parent, frame_id)
        return obj

    async def _path(self, node, parent, frame_id, x, y, depth):
        d = node.get("d")
        if not d:
            raise PenpotError("path node needs 'd'")
        w, h = _num(node, "w", "width"), _num(node, "h", "height")
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{node.get("viewbox", "0 0 24 24")}">'
               f'<path d="{d}"/></svg>')
        return await self._place_svg(svg, node, parent, frame_id, x, y, w, h)

    async def _svg(self, node, parent, frame_id, x, y, depth):
        markup = node.get("svg")
        icon = node.get("icon")
        size = _num(node, "size")
        w = _num(node, "w", "width", default=size)
        h = _num(node, "h", "height", default=size)
        if not markup:
            if icon:
                markup, credit = await fetch_icon(str(icon))
                if credit:
                    self.attributions.append(credit)
            elif node.get("url"):
                markup = await fetch_svg_url(str(node["url"]))
            else:
                raise PenpotError("an icon/svg node needs 'icon' (e.g. 'game-icons:spartan-helmet'), "
                                  "'svg' markup or 'url'")
        return await self._place_svg(markup, node, parent, frame_id, x, y, w, h)

    async def _place_svg(self, markup, node, parent, frame_id, x, y, w, h):
        parsed = penpot_svg.parse_svg(markup)
        for note in parsed.notes:
            if note not in self.notes:
                self.notes.append(note)
        if not w and not h:
            w = 24.0 if max(parsed.width, parsed.height) <= 64 else min(parsed.width, 240.0)
        groups, tw, th, _scale = penpot_svg.fit(parsed, x, y, w, h)
        color = norm_color(node.get("color"))
        stroke_color = norm_color(node.get("stroke_color")) or color
        name = str(node.get("name") or node.get("icon") or "Vector")
        shapes: List[dict] = []
        holder = parent
        group_obj = None
        if len(groups) > 1:
            group_obj = self._common(node, "group", name, x, y, tw, th)
            group_obj["shapes"] = []
            self._add(group_obj, parent, frame_id)
            holder = {"id": group_obj["id"], "frame": frame_id, "is_frame": False}
        for index, g in enumerate(groups):
            x0, y0, x1, y1 = penpot_svg.segments_bbox(g.segments)
            sw = g.paint.stroke_width if g.paint.stroke else 0.0
            obj = self._common({} if group_obj else node, "path",
                               name if not group_obj else f"{name} {index + 1}",
                               x0, y0, max(x1 - x0, 0.01), max(y1 - y0, 0.01))
            obj["content"] = penpot_svg.penpot_content(g.segments)
            fill = g.paint.fill
            if fill == "currentColor":
                fill = color or "#000000"
            elif color and node.get("recolor"):
                fill = color
            fills = _fills(fill, g.paint.fill_opacity * g.paint.opacity) if fill else []
            obj["fills"] = fills
            if g.paint.stroke:
                s = g.paint.stroke
                if s == "currentColor":
                    s = stroke_color or "#000000"
                elif stroke_color and node.get("recolor"):
                    s = stroke_color
                caps = {"round": "round", "square": "square"}.get(g.paint.linecap)
                stroke = {"stroke-color": norm_color(s, "#000000"),
                          "stroke-opacity": g.paint.stroke_opacity,
                          "stroke-width": round(sw, 3), "stroke-style": "solid",
                          "stroke-alignment": "center"}
                if caps:
                    stroke["stroke-cap-start"] = caps
                    stroke["stroke-cap-end"] = caps
                obj["strokes"] = [stroke]
            if g.paint.fill_rule == "evenodd":
                obj["svg-attrs"] = {"fill-rule": "evenodd"}
            self._add(obj, holder, frame_id)
            shapes.append(obj)
        return group_obj or shapes[0]

    async def _group(self, node, parent, frame_id, x, y, depth):
        children = node.get("children") or []
        if not children:
            raise PenpotError("group needs children")
        start = len(self.created)
        obj = self._common(node, "group", "Group", x, y, 1, 1)
        obj["shapes"] = []
        self._add(obj, parent, frame_id)
        me = {"id": obj["id"], "frame": frame_id, "is_frame": False}
        for child in children:
            await self.build(child, me, x, y, depth + 1)
        kids = [c for c in self.created[start + 1:] if c["parent"] == obj["id"]]
        if kids:
            gx = min(k["x"] for k in kids)
            gy = min(k["y"] for k in kids)
            gx2 = max(k["x"] + k["width"] for k in kids)
            gy2 = max(k["y"] + k["height"] for k in kids)
            obj.update(_rect_geom(gx, gy, gx2 - gx, gy2 - gy))
            self.created[start].update(x=round(gx, 1), y=round(gy, 1),
                                       width=round(gx2 - gx, 1), height=round(gy2 - gy, 1))
        return obj


def _find_parent(file: dict, page_id: str, parent_id: Optional[str]) -> Tuple[dict, Tuple[float, float]]:
    pages = (file.get("data") or {}).get("pagesIndex") or {}
    page = pages.get(page_id)
    if page is None:
        raise PenpotError(f"page {page_id} is not in this file. Pages: " +
                          ", ".join(f"{pid} ({p.get('name')})" for pid, p in pages.items()))
    if not parent_id or parent_id == ROOT_ID:
        return {"id": ROOT_ID, "frame": ROOT_ID, "is_frame": True}, (0.0, 0.0)
    obj = (page.get("objects") or {}).get(parent_id)
    if obj is None:
        raise PenpotError(f"parent shape {parent_id} is not on page {page_id}")
    kind = obj.get("type")
    if kind not in ("frame", "group"):
        raise PenpotError(f"parent shape {parent_id} is a {kind}; children go inside a frame or group")
    frame = obj["id"] if kind == "frame" else obj.get("frameId", ROOT_ID)
    return {"id": obj["id"], "frame": frame, "is_frame": kind == "frame"}, (
        float(obj.get("x", 0)), float(obj.get("y", 0)))


async def build_tree(client: PenpotClient, file_id: str, page_id: str,
                     nodes: List[dict], parent_id: Optional[str] = None,
                     dry_run: bool = False) -> dict:
    """Add ``nodes`` (positions relative to the parent) in one atomic update."""
    if not isinstance(nodes, list) or not nodes:
        raise PenpotError("'nodes' must be a non-empty list")
    file = await client.get_file(file_id)
    parent, origin = _find_parent(file, page_id, parent_id)
    ignored: List[str] = []
    nodes = normalize_nodes(nodes, ignored)
    texts: List[dict] = []
    _collect_text_nodes(nodes, texts)
    measured: Dict[int, penpot_text.Measured] = {}
    if texts:
        for node, m in zip(texts, await penpot_text.measure([text_spec(n) for n in texts])):
            measured[id(node)] = m
    builder = _Builder(page_id, parent, origin, measured)
    for node in nodes:
        await builder.build(node, parent, origin[0], origin[1])
    if not dry_run:
        await client.apply(file_id, builder.changes)
    return {
        "created": builder.created, "count": len(builder.created),
        "attributions": sorted(set(builder.attributions)), "applied": not dry_run,
        "text_measured_in_browser": bool(texts) and builder.inexact_text == 0,
        "warnings": ([f"{builder.inexact_text} text node(s) were sized by estimate (no browser or font "
                      "available to measure); they may look stretched or squeezed until opened in Penpot"]
                     if builder.inexact_text else []) + [f"svg: {n}" for n in builder.notes] + ignored,
    }


async def move_shapes(client: PenpotClient, file_id: str, page_id: str,
                      shape_ids: List[str], parent_id: str) -> dict:
    """Re-parent existing shapes (what the stock tools cannot: they always
    write to the root frame). Absolute positions are kept."""
    file = await client.get_file(file_id)
    parent, _ = _find_parent(file, page_id, parent_id)
    if parent["id"] == ROOT_ID:
        raise PenpotError("moving shapes to the page root is not supported here")
    objects = file["data"]["pagesIndex"][page_id]["objects"]
    missing = [s for s in shape_ids if s not in objects]
    if missing:
        raise PenpotError(f"shapes not on page {page_id}: {', '.join(missing)}")
    changes = [{
        "type": "mov-objects", "page-id": page_id, "parent-id": parent["id"],
        "frame-id": parent["id"] if parent["is_frame"] else parent["frame"],
        "shapes": shape_ids,
    }]
    await client.apply(file_id, changes)
    return {"moved": shape_ids, "into": parent["id"]}


# ---------------------------------------------------------------------------
# Icon library (Iconify)
# ---------------------------------------------------------------------------

async def _get(url: str, *, params: Optional[dict] = None, timeout: float = 20.0) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     headers={"User-Agent": "Odysseus-PenpotStudio/1.0"}) as http:
            resp = await http.get(url, params=params)
    except httpx.HTTPError as exc:
        raise PenpotError(f"could not fetch {url}: {type(exc).__name__}: {exc}") from exc
    if resp.status_code >= 400:
        raise PenpotError(f"{url} answered HTTP {resp.status_code}")
    return resp


async def _search_once(query: str, prefix: Optional[str], limit: int) -> dict:
    params: Dict[str, Any] = {"query": query, "limit": limit}
    if prefix:
        params["prefix"] = prefix
    return (await _get(f"{ICONIFY_API}/search", params=params)).json()


ICON_ARTWORK_MAX_IDS = 6
ICON_ARTWORK_MAX_SVG = 24_000      # bytes per icon; game-icons run ~1-6 KB, a few detailed ones 20 KB
ICON_ARTWORK_MAX_TOTAL = 80_000    # bytes per call, so one reply cannot flood the model's context


async def icon_artwork(ids: Iterable[str]) -> dict:
    """Full standalone SVG for chosen icon ids, with licence and a pasteable credit.

    2026-10-01: designers and engineers asked for logos/illustrations and
    hand-drew SVG paths because search_icons only returned ids. This hands back
    the real drawing (Iconify's ``/<prefix>/<name>.svg``) so it can be dropped
    into a page, a file or build_design's ``svg`` node, plus what the licence
    needs: author, source URL and an ACKNOWLEDGMENTS line.
    """
    wanted: List[str] = []
    for raw in ids or []:
        icon = str(raw).strip().lower()
        if icon and icon not in wanted:
            wanted.append(icon)
    if not wanted:
        raise PenpotError("pass at least one icon id like 'game-icons:spartan-helmet' (find ids with search_icons)")
    skipped = wanted[ICON_ARTWORK_MAX_IDS:]
    wanted = wanted[:ICON_ARTWORK_MAX_IDS]
    results: List[dict] = []
    sets: Dict[str, dict] = {}
    total = 0
    for icon in wanted:
        m = re.fullmatch(r"([a-z0-9][a-z0-9-]*):([a-z0-9][a-z0-9-]*)", icon)
        if not m:
            results.append({"id": icon, "error": "ids look like 'game-icons:spartan-helmet'"})
            continue
        prefix, name = m.groups()
        source = f"https://icon-sets.iconify.design/{prefix}/{name}/"
        try:
            svg = (await _get(f"{ICONIFY_API}/{prefix}/{name}.svg")).text
        except PenpotError as exc:
            hint = " Icon ids are exact; copy one from search_icons." if "404" in str(exc) else ""
            results.append({"id": icon, "error": str(exc) + hint})
            continue
        if "<svg" not in svg:
            results.append({"id": icon, "error": "Iconify returned no drawing for this id"})
            continue
        if len(svg) > ICON_ARTWORK_MAX_SVG or total + len(svg) > ICON_ARTWORK_MAX_TOTAL:
            results.append({"id": icon, "source_url": source,
                            "error": f"drawing is {len(svg)} bytes, over this call's size cap; "
                                     "ask for fewer icons per call or pick a simpler one"})
            continue
        total += len(svg)
        if prefix not in sets:
            try:
                sets[prefix] = (await _get(f"{ICONIFY_API}/collections",
                                           params={"prefixes": prefix})).json().get(prefix) or {}
            except PenpotError:
                sets[prefix] = {}
        info = sets[prefix]
        lic = info.get("license") or {}
        author = info.get("author") or {}
        lic_name = lic.get("title") or lic.get("spdx") or "licence unknown (check the source page before shipping)"
        credit = (f"{info.get('name', prefix)}: {name} by {author.get('name') or 'its authors'} "
                  f"({lic_name}) {author.get('url') or lic.get('url') or source}")
        results.append({
            "id": icon, "svg": svg, "bytes": len(svg),
            "license": {"title": lic.get("title"), "spdx": lic.get("spdx"), "url": lic.get("url")},
            "author": {"name": author.get("name"), "url": author.get("url")},
            "source_url": source,
            "attribution_required": _needs_attribution(lic),
            "attribution": credit,
        })
    out: Dict[str, Any] = {"artwork": results}
    if skipped:
        out["not_fetched"] = skipped
        out["note"] = f"at most {ICON_ARTWORK_MAX_IDS} icons per call; ask again for the rest"
    return out


async def search_icons(query: str, prefix: Optional[str] = None, limit: int = 24) -> dict:
    """Search open icon sets. Returns ids like ``game-icons:spartan-helmet`` plus
    each set's licence so the designer can attribute correctly.

    Iconify matches every word, so "greek soldier helmet" finds nothing while
    "helmet" finds dozens (the 2026-09-30 designer concluded there were no
    distinct helmet profiles). When a multi-word query is thin, each word is
    searched too and ids matching more words come first.
    """
    limit = max(1, min(int(limit), 64))
    data = await _search_once(query, prefix, limit)
    icons = list(data.get("icons", []))
    collections = dict(data.get("collections") or {})
    total = data.get("total", 0)
    words = [w for w in re.split(r"[\s,]+", query.lower()) if len(w) > 2]
    broadened = False
    if len(icons) < limit and len(words) > 1:
        found = {i: sum(w in i for w in words) for i in icons}
        for word in words:
            extra = await _search_once(word, prefix, limit)
            collections.update(extra.get("collections") or {})
            for i in extra.get("icons", []):
                found.setdefault(i, sum(w in i for w in words))
        icons = sorted(found, key=lambda i: -found[i])[:limit]
        broadened = True
    sets = {}
    for pfx, info in collections.items():
        if not any(i.startswith(pfx + ":") for i in icons):
            continue
        lic = info.get("license") or {}
        author = info.get("author") or {}
        sets[pfx] = {
            "name": info.get("name"), "author": author.get("name"),
            "license": lic.get("title") or lic.get("spdx"),
            "attribution_required": _needs_attribution(lic),
            "monotone": not info.get("palette", False),
        }
    out = {"icons": icons, "total": total, "sets": sets}
    if broadened:
        out["note"] = ("no icon matched every word; results match at least one word, best matches first. "
                       "Try single words, or pass 'set' (e.g. 'game-icons') to browse one library.")
    return out


def _needs_attribution(lic: dict) -> bool:
    spdx = str(lic.get("spdx") or lic.get("title") or "").lower()
    return not any(tag in spdx for tag in ("mit", "isc", "0bsd", "cc0", "unlicense", "public"))


async def fetch_icon(icon: str) -> Tuple[str, Optional[str]]:
    """``prefix:name`` -> (svg markup, attribution line or None)."""
    m = re.fullmatch(r"([a-z0-9][a-z0-9-]*):([a-z0-9][a-z0-9-]*)", icon.strip().lower())
    if not m:
        raise PenpotError(f"icon {icon!r} must look like 'game-icons:spartan-helmet' "
                          "(use penpot_search_icons to find ids)")
    prefix, name = m.groups()
    try:
        svg = (await _get(f"{ICONIFY_API}/{prefix}/{name}.svg")).text
    except PenpotError as exc:
        if "404" in str(exc):
            raise PenpotError(
                f"icon {icon} does not exist. Icon ids are exact: use search_icons "
                f"(query '{name.split('-')[-1]}', set '{prefix}') and copy an id it returns.") from exc
        raise
    if "<svg" not in svg:
        raise PenpotError(f"icon {icon} does not exist (Iconify returned no drawing)")
    credit = None
    try:
        info = (await _get(f"{ICONIFY_API}/collections", params={"prefixes": prefix})).json().get(prefix) or {}
        lic = info.get("license") or {}
        if _needs_attribution(lic):
            author = (info.get("author") or {}).get("name", "the icon set's authors")
            credit = f"{info.get('name', prefix)} by {author} ({lic.get('title') or lic.get('spdx')}) via iconify.design"
    except PenpotError:
        credit = f"{prefix} icon set via iconify.design (check its licence before shipping)"
    return svg, credit


def _ip_is_public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified)


async def _resolve(host: str, port: int) -> List[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def _assert_public(url: str) -> None:
    """Refuse a URL unless every address its host resolves to is public."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise PenpotError("svg url must be http(s)")
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        raise PenpotError("svg url points at a private address; only public URLs are fetched")
    try:
        addrs = await _resolve(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except OSError as exc:
        raise PenpotError(f"cannot resolve {host}: {exc}") from exc
    if not addrs or not all(_ip_is_public(a) for a in addrs):
        raise PenpotError("svg url points at a private address; only public URLs are fetched")


async def fetch_svg_url(url: str, max_bytes: int = 2_000_000) -> str:
    """Fetch a public SVG. Redirects are followed by hand so each hop is
    re-validated (a public URL must not bounce us to an internal one)."""
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False,
                                 headers={"User-Agent": "Odysseus-PenpotStudio/1.0"}) as http:
        for _ in range(5):
            await _assert_public(url)
            try:
                resp = await http.get(url)
            except httpx.HTTPError as exc:
                raise PenpotError(f"could not fetch {url}: {type(exc).__name__}: {exc}") from exc
            if resp.is_redirect and resp.headers.get("location"):
                url = str(resp.url.join(resp.headers["location"]))
                continue
            break
        else:
            raise PenpotError("too many redirects fetching the svg url")
    if resp.status_code >= 400:
        raise PenpotError(f"{url} answered HTTP {resp.status_code}")
    if len(resp.content) > max_bytes:
        raise PenpotError("that SVG is too large to import")
    text = resp.text
    if "<svg" not in text.lower():
        raise PenpotError("that URL did not return SVG markup (an HTML page, not the raw .svg file?)")
    return text


# ---------------------------------------------------------------------------
# Reading back: layout lint
# ---------------------------------------------------------------------------

def _box(obj: dict) -> Tuple[float, float, float, float]:
    sel = obj.get("selrect") or {}
    x = float(sel.get("x", obj.get("x", 0)))
    y = float(sel.get("y", obj.get("y", 0)))
    return x, y, float(sel.get("width", obj.get("width", 0))), float(sel.get("height", obj.get("height", 0)))


def _inter(a: Tuple[float, ...], b: Tuple[float, ...]) -> float:
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def _text_leaves(node: Any) -> Iterable[Tuple[str, dict]]:
    """``(text, style)`` for every text run in a Penpot text ``content`` tree."""
    if isinstance(node, dict):
        if "text" in node and not node.get("children"):
            yield str(node.get("text") or ""), node
        for child in node.get("children") or []:
            yield from _text_leaves(child)


def describe_page(file: dict, page_id: str, frame_id: Optional[str] = None,
                  tolerance: float = 1.0) -> dict:
    """Tree outline plus layout problems for a page (or one board).

    Problems: a child that sticks out of its board (clipped by the board), and
    two content shapes in the same board that partly overlap (neither contains
    the other, so it is not just a label sitting on a card). Text bounds are
    Penpot's stored ones: approximate until the file has been opened in Penpot.
    """
    pages = (file.get("data") or {}).get("pagesIndex") or {}
    page = pages.get(page_id)
    if page is None:
        raise PenpotError(f"page {page_id} is not in this file")
    objects: Dict[str, dict] = page.get("objects") or {}
    start = frame_id or ROOT_ID
    if start not in objects:
        raise PenpotError(f"shape {start} is not on page {page_id}")

    outline: List[dict] = []
    problems: List[str] = []
    palette: Dict[str, int] = {}
    fonts: set = set()

    def label(o: dict) -> str:
        return f"{o.get('type')} '{o.get('name')}' ({o.get('id')})"

    def walk(oid: str, depth: int) -> None:
        o = objects[oid]
        x, y, w, h = _box(o)
        if oid != ROOT_ID:
            entry = {"id": oid, "type": o.get("type"), "name": o.get("name"),
                     "depth": depth, "x": round(x, 1), "y": round(y, 1),
                     "w": round(w, 1), "h": round(h, 1)}
            fill = next((f.get("fillColor") for f in o.get("fills") or [] if f.get("fillColor")), None)
            if fill:
                entry["fill"] = fill
                palette[fill] = palette.get(fill, 0) + 1
            stroke = next(((s.get("strokeColor"), s.get("strokeWidth"))
                           for s in o.get("strokes") or [] if s.get("strokeColor")), None)
            if stroke:
                entry["stroke"] = f"{stroke[0]} {stroke[1]}px"
            if o.get("type") == "text":
                leaves = list(_text_leaves(o.get("content")))
                entry["text"] = " / ".join(t for t, _ in leaves)[:200]
                if leaves:
                    lf = leaves[0][1]
                    entry["font"] = f"{lf.get('fontFamily')} {lf.get('fontWeight')} {lf.get('fontSize')}px"
                    fonts.add(str(lf.get("fontFamily")))
                    color = next((f.get("fillColor") for f in lf.get("fills") or []), None)
                    if color:
                        entry["color"] = color
                        palette[color] = palette.get(color, 0) + 1
                if not o.get("positionData"):
                    problems.append(f"NO-RENDER: text {label(o)} has no position-data, so Penpot's "
                                    "viewer and exporter draw nothing for it")
            outline.append(entry)
        kids = [k for k in (o.get("shapes") or []) if k in objects]
        if o.get("type") == "frame" and oid != ROOT_ID:
            boxes = {k: _box(objects[k]) for k in kids}
            for k, (kx, ky, kw, kh) in boxes.items():
                if kx < x - tolerance or ky < y - tolerance or kx + kw > x + w + tolerance or ky + kh > y + h + tolerance:
                    problems.append(f"OVERFLOW: {label(objects[k])} extends outside board '{o.get('name')}' "
                                    f"(child {round(kx)},{round(ky)} {round(kw)}x{round(kh)}; board "
                                    f"{round(x)},{round(y)} {round(w)}x{round(h)})")
            ids = list(boxes)
            for i, a in enumerate(ids):
                for b in ids[i + 1:]:
                    ba, bb = boxes[a], boxes[b]
                    inter = _inter(ba, bb)
                    if inter <= 0:
                        continue
                    smaller = min(ba[2] * ba[3], bb[2] * bb[3]) or 1.0
                    if inter >= smaller - 1.0:  # one contains the other: a label on a card
                        continue
                    kinds = {objects[a].get("type"), objects[b].get("type")}
                    if "text" in kinds or inter / smaller > 0.15:
                        problems.append(f"OVERLAP: {label(objects[a])} and {label(objects[b])} overlap by "
                                        f"{round(100 * inter / smaller)}% of the smaller shape")
        for k in kids:
            walk(k, depth + 1)

    walk(start, 0)
    return {"shapes": outline, "problems": problems, "shape_count": len(outline),
            "palette": dict(sorted(palette.items(), key=lambda kv: -kv[1])[:12]),
            "fonts": sorted(fonts)}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

async def share_link(client: PenpotClient, file_id: str, page_id: str) -> str:
    """Mint a view-only share id for one page; the caller deletes it."""
    link = await client.rpc("create-share-link", {
        "file-id": file_id, "who-comment": "team", "who-inspect": "team", "pages": [page_id]})
    sid = (link or {}).get("id")
    if not sid:
        raise PenpotError(f"Penpot returned no share link id: {link!r}")
    return sid


def viewer_url(cfg: PenpotConfig, file_id: str, page_id: str, frame_id: str, share_id: str,
               origin: Optional[str] = None) -> str:
    return (f"{origin or cfg.public_url}/#/view?file-id={file_id}&page-id={page_id}"
            f"&section=interactions&frame-id={frame_id}&index=0&share-id={share_id}")


_PUBLIC_URI_RE = re.compile(r"""penpotPublicURI\s*=\s*(["'])(.*?)\1""")
_public_uri_cache: Dict[str, Optional[str]] = {}


def parse_public_uri(config_js: str) -> Optional[str]:
    """``penpotPublicURI`` from Penpot's ``/js/config.js`` as a bare origin, or None."""
    m = _PUBLIC_URI_RE.search(config_js or "")
    if not m:
        return None
    parsed = urlparse(m.group(2).strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


async def fetch_public_uri(base_url: str) -> Optional[str]:
    """The origin Penpot's frontend calls its own API on (cached per base URL).

    2026-10-01 (ZimaOS): Odysseus is configured with the NAS LAN IP, but that
    frontend advertises ``penpotPublicURI = "http://homelab.nas:9001"``, a
    Pi-hole name that does not resolve inside the container. The viewer then
    failed every API/asset request and Penpot drew its error page, which we
    screenshotted and called a success. A failed fetch is not cached, so a
    later render retries.
    """
    if base_url in _public_uri_cache:
        return _public_uri_cache[base_url]
    try:
        async with httpx.AsyncClient(timeout=4.0, follow_redirects=True) as http:
            resp = await http.get(f"{base_url}/js/config.js")
        if resp.status_code >= 400:
            return None
    except httpx.HTTPError:
        return None
    uri = parse_public_uri(resp.text)
    _public_uri_cache[base_url] = uri
    return uri


def _hostport(url: str) -> Tuple[str, int]:
    p = urlparse(url)
    return (p.hostname or "").lower(), p.port or (443 if p.scheme == "https" else 80)


def viewer_origin(cfg: PenpotConfig, public_uri: Optional[str]) -> Tuple[str, List[str]]:
    """(origin to open the viewer at, extra Chromium flags).

    ``PENPOT_PUBLIC_URL`` is an explicit operator choice and wins; otherwise the
    frontend's own ``penpotPublicURI`` is used so the page is same-origin with
    the API it calls. When that host:port is not the one Odysseus reaches
    Penpot on, Chromium is told to resolve it to the configured host.
    """
    explicit = os.environ.get("PENPOT_PUBLIC_URL", "").strip().rstrip("/")
    origin = explicit or public_uri or cfg.base_url
    pub_host, pub_port = _hostport(origin)
    api_host, api_port = _hostport(cfg.base_url)
    if not pub_host or (pub_host, pub_port) == (api_host, api_port):
        return origin, []
    target = f"[{api_host}]" if ":" in api_host else api_host
    return origin, [f"--host-resolver-rules=MAP {pub_host}:{pub_port} {target}:{api_port}"]


# Markers of Penpot's static exception screen (frontend/src/app/main/ui/static.cljs
# and translations/en.po, checked against Penpot develop 2026-10-01). The layout
# class is the reliable one; the English strings only count when the viewer's own
# markup is absent, because a mockup of an error screen can contain the same words.
# Penpot 2.17 (the user's NAS, 2026-10-01) reports a viewer that cannot load its
# data with an error-level toast instead ("Something wrong has happened.", class
# main_ui_ds_notifications_toast__level-error), and the word "viewer" occurs in
# its scripts, so the text rule alone missed it.
_ERROR_CLASS_RE = re.compile(r"exception[-_](?:layout|content)|notifications_toast__level-error")
_ERROR_TEXTS = ("Something wrong has happened", "Something bad happened", "Internal Error",
                "This page doesn't exist", "Bad Gateway", "Service Unavailable", "Oops!")


def penpot_error_page(dom: str) -> Optional[str]:
    """What Penpot's error screen says if ``dom`` is that screen, else None."""
    texts = [t for t in _ERROR_TEXTS if t in dom]
    if not _ERROR_CLASS_RE.search(dom) and (not texts or "viewer" in dom.lower()):
        return None
    visible = re.sub(r"<(script|style)\b.*?</\1>", " ", dom, flags=re.S | re.I)
    visible = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", visible)).strip()
    return visible[:200] or (texts[0] if texts else "exception page")


async def _run_browser(args: List[str], timeout: float) -> Tuple[bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise PenpotError("rendering timed out; Penpot's viewer did not finish loading")
    return out or b"", err or b""


def find_boards(file: dict, page_id: str) -> List[dict]:
    page = ((file.get("data") or {}).get("pagesIndex") or {}).get(page_id) or {}
    objects = page.get("objects") or {}
    root = objects.get(ROOT_ID) or {}
    out = []
    for k in root.get("shapes") or []:
        o = objects.get(k) or {}
        if o.get("type") == "frame":
            x, y, w, h = _box(o)
            out.append({"id": k, "name": o.get("name"), "x": x, "y": y, "w": w, "h": h})
    return out


browser_executable = penpot_text.browser_executable


async def render_board(client: PenpotClient, file_id: str, page_id: str,
                       frame_id: Optional[str], out_png: str, *, width: Optional[int] = None,
                       height: Optional[int] = None, wait_ms: int = 12000) -> dict:
    """Screenshot a board in Penpot's own viewer with headless Chromium.

    The viewer renders with Penpot's real engine and fonts, so this shows what
    the user will see. It needs only the access token (to mint a view-only
    share link) and a Chromium on the same network as Penpot.
    """
    file = await client.get_file(file_id)
    boards = find_boards(file, page_id)
    if not boards:
        raise PenpotError(f"page {page_id} has no boards (top-level frames) to render; "
                          "put the design inside a board")
    board = next((b for b in boards if b["id"] == frame_id), None) if frame_id else boards[0]
    if board is None:
        raise PenpotError(f"{frame_id} is not a top-level board on that page. Boards: " +
                          ", ".join(f"{b['id']} ({b['name']})" for b in boards))
    sid = await share_link(client, file_id, page_id)
    origin, resolver_flags = viewer_origin(client.cfg, await fetch_public_uri(client.cfg.base_url))
    url = viewer_url(client.cfg, file_id, page_id, board["id"], sid, origin)
    keep_link = False
    try:
        w = int(width or min(max(board["w"] + 80, 480), 2400))
        h = int(height or min(max(board["h"] + 120, 360), 2400))
        exe = browser_executable()
        if not exe:
            keep_link = True  # the agent is being handed this link to open itself
            raise PenpotError("no Chromium/Chrome found for rendering. Open this view-only link in the "
                              f"browser tool and take a screenshot instead: {url}")
        if os.path.exists(out_png):
            os.remove(out_png)
        timeout = wait_ms / 1000 + 45
        with tempfile.TemporaryDirectory(prefix="penpot-render-profile-") as profile, \
                tempfile.TemporaryDirectory(prefix="penpot-render-dom-") as dom_profile:
            def argv(user_dir: str, mode: str) -> List[str]:
                return [exe, "--headless=new", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage",
                        "--hide-scrollbars", "--force-device-scale-factor=1", f"--window-size={w},{h}",
                        f"--user-data-dir={user_dir}", f"--virtual-time-budget={wait_ms}",
                        *resolver_flags, mode, url]
            # A screenshot cannot tell Penpot's error screen from a design, so a
            # second run dumps the DOM under the same budget. They run side by
            # side (separate profiles) to keep the render time flat.
            (_, err), (dom, _) = await asyncio.gather(
                _run_browser(argv(profile, f"--screenshot={out_png}"), timeout),
                _run_browser(argv(dom_profile, "--dump-dom"), timeout))
        bad = penpot_error_page(dom.decode("utf-8", "replace"))
        if bad:
            if os.path.exists(out_png):
                os.remove(out_png)
            mismatch = (f" The viewer was opened at {origin} (penpotPublicURI / PENPOT_PUBLIC_URL) "
                        f"while Agamemnon reaches Penpot at {client.cfg.base_url}."
                        if origin != client.cfg.base_url else "")
            raise PenpotError(
                f"Penpot's viewer could not load the board; it showed its error page ({bad!r}).{mismatch} "
                "Usually the browser cannot reach the host the Penpot frontend calls (set PENPOT_PUBLIC_URL "
                "to an address reachable from Agamemnon) or the file/share link is gone. "
                "No screenshot was returned; use inspect_design for the shape data meanwhile.")
        if not os.path.isfile(out_png) or os.path.getsize(out_png) < 1000:
            raise PenpotError("the browser produced no screenshot: " + (err or b"").decode("utf-8", "replace")[-300:])
    finally:
        if not keep_link:
            try:
                await client.rpc("delete-share-link", {"id": sid})
            except PenpotError:
                pass  # a stale view-only link to a LAN Penpot is low risk; nothing to do
    return {"path": out_png, "board": board, "viewer_url": url, "width": w, "height": h}
