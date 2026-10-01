"""preview_file: render a workspace file to a PNG the model can see.

The subprocess is mocked; nothing here needs a browser. (2026-10-01: an
"Agamemnon helmet" SVG that was really headphones passed every string check
because no engineering agent could look at what it built.)
"""

import asyncio
import base64
import io
import json

import pytest

from src.agent_tools import TOOL_HANDLERS, preview_tools
from src.agent_tools.preview_tools import (
    MAX_RETURN_SIDE,
    MAX_VIEWPORT,
    build_chromium_argv,
    model_image_followup,
)
from src.tool_execution import _active_workspace
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block

PIL = pytest.importorskip("PIL.Image")


def _png(width=4, height=3, color=(200, 30, 30)) -> bytes:
    out = io.BytesIO()
    PIL.new("RGB", (width, height), color).save(out, format="PNG")
    return out.getvalue()


def _read_url(url):
    """Fetch from the preview server the way the browser would (no proxy)."""
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=5) as response:
        return response.read().decode("utf-8")


def _run(args):
    return asyncio.run(TOOL_HANDLERS["preview_file"](json.dumps(args), {}))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    token = _active_workspace.set(str(ws))
    yield ws
    _active_workspace.reset(token)


def test_registered_with_schema_and_dispatch():
    names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert "preview_file" in names
    schema = next(s for s in FUNCTION_TOOL_SCHEMAS if s["function"]["name"] == "preview_file")
    assert "compare" in schema["function"]["description"]
    block = function_call_to_tool_block("preview_file", {"path": "a.svg", "width": 64})
    assert block.tool_type == "preview_file"
    assert json.loads(block.content) == {"path": "a.svg", "width": 64}


def test_argv_flags_and_viewport():
    argv = build_chromium_argv("chrome", "http://127.0.0.1:1/x.html", "/o.png", "/p",
                               width=1280, height=800, server_port=4321)
    assert argv[0] == "chrome" and argv[-1] == "http://127.0.0.1:1/x.html"
    for flag in ("--headless=new", "--no-sandbox", "--disable-gpu", "--window-size=1280,800",
                 "--screenshot=/o.png", "--user-data-dir=/p", "--force-device-scale-factor=1"):
        assert flag in argv
    assert any(a.startswith("--host-resolver-rules=MAP * ~NOTFOUND") for a in argv)
    # Network lock: everything goes to a dead proxy, bypassed only for the
    # preview origin, and <-loopback> removes Chrome's implicit loopback bypass.
    assert "--proxy-server=http://127.0.0.1:9" in argv
    assert "--proxy-bypass-list=<-loopback>;127.0.0.1:4321" in argv
    assert not any("EXCLUDE localhost" in a for a in argv)
    bare = build_chromium_argv("c", "u", "o", "p", width=10, height=10)
    assert "--proxy-bypass-list=<-loopback>" in bare


def test_argv_dark_and_light_scheme():
    dark = build_chromium_argv("c", "u", "o", "p", width=10, height=10, color_scheme="dark")
    light = build_chromium_argv("c", "u", "o", "p", width=10, height=10, color_scheme="light")
    assert "--blink-settings=preferredColorScheme=0" in dark
    assert "--blink-settings=preferredColorScheme=1" in light
    assert "--force-dark-mode" not in dark


def test_missing_browser_names_the_env_var(workspace, monkeypatch):
    (workspace / "a.html").write_text("<p>hi</p>")
    monkeypatch.setattr(preview_tools, "find_browser", lambda: "")
    result = _run({"path": "a.html"})
    assert result["exit_code"] == 1
    assert "ODYSSEUS_BROWSER_EXECUTABLE" in result["error"]


def test_png_passes_through_without_a_browser(workspace, monkeypatch):
    monkeypatch.setattr(preview_tools, "find_browser", lambda: pytest.fail("no browser for images"))
    data = _png()
    (workspace / "icon.png").write_bytes(data)
    result = _run({"path": "icon.png"})
    assert result["exit_code"] == 0
    image = result["images"][0]
    assert image["mimeType"] == "image/png"
    assert base64.b64decode(image["data"]) == data  # byte-for-byte


def test_huge_image_is_resized_and_gif_becomes_png(workspace):
    big = io.BytesIO()
    PIL.new("RGB", (MAX_RETURN_SIDE * 2, 100), (1, 2, 3)).save(big, format="PNG")
    (workspace / "big.png").write_bytes(big.getvalue())
    out = _run({"path": "big.png"})
    resized = PIL.open(io.BytesIO(base64.b64decode(out["images"][0]["data"])))
    assert max(resized.size) == MAX_RETURN_SIDE

    gif = io.BytesIO()
    PIL.new("P", (8, 8)).save(gif, format="GIF")
    (workspace / "a.gif").write_bytes(gif.getvalue())
    assert _run({"path": "a.gif"})["images"][0]["mimeType"] == "image/png"


def test_refuses_outside_workspace(workspace, tmp_path):
    outside = tmp_path / "secret.png"
    outside.write_bytes(_png())
    result = _run({"path": str(outside)})
    assert result["exit_code"] == 1 and "outside" in result["error"]
    assert "images" not in result


def test_refuses_sensitive_paths(workspace):
    ssh = workspace / ".ssh"
    ssh.mkdir()
    (ssh / "x.png").write_bytes(_png())
    result = _run({"path": ".ssh/x.png"})
    assert result["exit_code"] == 1 and "sensitive" in result["error"]
    assert "images" not in result


def test_refuses_directory_unsupported_type_and_oversize(workspace, monkeypatch):
    (workspace / "d").mkdir()
    assert "directory" in _run({"path": "d"})["error"]
    (workspace / "a.txt").write_text("x")
    assert "unsupported" in _run({"path": "a.txt"})["error"]
    monkeypatch.setattr(preview_tools, "MAX_MARKUP_BYTES", 10)
    (workspace / "big.html").write_text("<p>" + "x" * 100)
    assert "limit" in _run({"path": "big.html"})["error"]


def test_html_render_uses_capped_viewport_and_cleans_up(workspace, monkeypatch):
    (workspace / "a.html").write_text("<p>hi</p>")
    seen = {}

    class Proc:
        async def communicate(self):
            return b"", b""

    async def fake_exec(*argv, **kwargs):
        seen["argv"] = list(argv)
        seen["html"] = _read_url(argv[-1])
        seen["sub"] = _read_url(argv[-1].rsplit("/", 2)[0] + "/a.html")
        out = next(a for a in argv if a.startswith("--screenshot=")).split("=", 1)[1]
        with open(out, "wb") as handle:
            handle.write(_png(MAX_VIEWPORT * 4 + 64, 350 * 4))
        return Proc()

    monkeypatch.setattr(preview_tools, "find_browser", lambda: "chrome")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = _run({"path": "a.html", "width": 99999, "height": 50, "color_scheme": "dark", "scale": 9})
    assert result["exit_code"] == 0, result
    argv = seen["argv"]
    # The window is bigger than the viewport (Windows headless sizes the outer
    # window); the viewport itself is the wrapper iframe's size.
    assert f"--window-size={MAX_VIEWPORT + 64},350" in argv
    assert f"width:{MAX_VIEWPORT}px;height:50px" in seen["html"]
    assert "--force-device-scale-factor=4" in argv
    assert "--blink-settings=preferredColorScheme=0" in argv
    assert argv[-1].startswith("http://127.0.0.1:") and argv[-1].endswith(preview_tools.WRAPPER_PATH)
    port = argv[-1].split(":")[2].split("/")[0]
    assert f"--proxy-bypass-list=<-loopback>;127.0.0.1:{port}" in argv
    # Same-origin root-relative target, never a file:// URL.
    assert '<iframe src="/a.html"' in seen["html"] and "file:" not in seen["html"]
    assert seen["sub"] == "<p>hi</p>"
    # Cropped back to the requested viewport at 4x (10240x200), then shrunk to the return cap.
    shot = PIL.open(io.BytesIO(base64.b64decode(result["images"][0]["data"])))
    assert shot.size == (MAX_RETURN_SIDE, MAX_RETURN_SIDE * 200 // (MAX_VIEWPORT * 4))
    # The temp dir (profile + screenshot) is gone afterwards.
    import os

    shot = next(a for a in argv if a.startswith("--screenshot=")).split("=", 1)[1]
    assert not os.path.exists(os.path.dirname(shot))


def test_svg_is_wrapped_so_it_fills_the_viewport(workspace, monkeypatch):
    (workspace / "logo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"/>')
    seen = {}

    class Proc:
        async def communicate(self):
            return b"", b""

    async def fake_exec(*argv, **kwargs):
        out = next(a for a in argv if a.startswith("--screenshot=")).split("=", 1)[1]
        wrapper = argv[-1]
        seen["argv"] = list(argv)
        seen["html"] = _read_url(wrapper)
        with open(out, "wb") as handle:
            handle.write(_png(256, 256))
        return Proc()

    monkeypatch.setattr(preview_tools, "find_browser", lambda: "chrome")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = _run({"path": "logo.svg", "width": 256, "height": 256})
    assert result["exit_code"] == 0, result
    assert "--window-size=520,556" in seen["argv"]  # 256 wide, padded past the 520px minimum
    assert 'src="/logo.svg"' in seen["html"] and "<img" in seen["html"]


def test_browser_failure_and_timeout_are_reported(workspace, monkeypatch):
    (workspace / "a.html").write_text("<p>hi</p>")

    class Proc:
        killed = False

        async def communicate(self):
            return b"", b"boom"

        def kill(self):
            self.killed = True

    async def fake_exec(*argv, **kwargs):
        return Proc()  # writes no screenshot

    monkeypatch.setattr(preview_tools, "find_browser", lambda: "chrome")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = _run({"path": "a.html"})
    assert result["exit_code"] == 1 and "no screenshot" in result["error"] and "boom" in result["error"]

    async def raising_exec(*argv, **kwargs):
        raise OSError("not executable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", raising_exec)
    assert "could not start" in _run({"path": "a.html"})["error"]


def test_model_image_followup_builds_an_image_message():
    records = [
        {"tool_name": "ls", "result": {"output": "x"}},
        {"tool_name": "preview_file", "result": {"images": [{"data": "QUJD", "mimeType": "image/png"}]}},
    ]
    message = model_image_followup(records)
    assert message["role"] == "user"
    parts = message["content"]
    assert parts[0]["type"] == "text"
    assert parts[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}
    assert model_image_followup([records[0]]) is None


# --- the confined preview server ------------------------------------------------

def _fetch(server, path):
    """(status, body, content-type) for a GET, without following a proxy."""
    import urllib.error
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{server.port}{path}", timeout=5) as response:
            return response.status, response.read(), response.headers.get("Content-Type")
    except urllib.error.HTTPError as err:
        return err.code, err.read(), err.headers.get("Content-Type")


@pytest.fixture
def served(workspace):
    started = []

    def make(**kwargs):
        server = preview_tools._PreviewServer(str(workspace), **kwargs).start()
        started.append(server)
        return server

    yield make
    for server in started:
        server.stop()


def test_server_serves_relative_and_root_relative_assets(workspace, served):
    (workspace / "static").mkdir()
    (workspace / "static" / "app.js").write_text("var a=1;")
    (workspace / "static" / "app.css").write_text("body{}")
    (workspace / "logo.svg").write_text("<svg/>")
    server = served()
    status, body, ctype = _fetch(server, "/static/app.js")  # what /static/app.js means in the repo
    assert (status, body) == (200, b"var a=1;") and ctype.startswith("text/javascript")
    assert _fetch(server, "/static/app.css")[2].startswith("text/css")
    assert _fetch(server, "/logo.svg")[2] == "image/svg+xml"
    assert _fetch(server, "/static/app.js?v=3")[0] == 200
    assert _fetch(server, "/static/%61pp.js")[0] == 200  # percent-decoded like a browser would


def test_wrapper_is_served_same_origin(workspace, served):
    server = served(wrapper_html="<iframe src='/a.html'>")
    status, body, ctype = _fetch(server, preview_tools.WRAPPER_PATH)
    assert status == 200 and body == b"<iframe src='/a.html'>" and ctype.startswith("text/html")
    assert server.wrapper_url.startswith(f"http://127.0.0.1:{server.port}/")


def test_server_refuses_traversal_outside_root_and_directories(workspace, tmp_path, served):
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    (workspace / "sub").mkdir()
    (workspace / "sub" / "ok.txt").write_text("fine")
    server = served()
    for path in ("/../secret.txt", "/sub/../../secret.txt", "/%2e%2e/secret.txt",
                 "/sub/%2e%2e/%2e%2e/secret.txt", "/..%2fsecret.txt", "/sub\\..\\..\\secret.txt",
                 "/sub", "/sub/", "/", "/nope.txt", "/%00"):
        status, body, _ = _fetch(server, path)
        assert status == 404 and b"SECRET" not in body, path
    # Absolute filesystem paths are just (missing) names under the root.
    assert _fetch(server, "/" + str(tmp_path / "secret.txt").replace("\\", "/"))[0] == 404
    assert _fetch(server, "/sub/ok.txt")[0] == 200


def test_server_refuses_symlink_out_of_root(workspace, tmp_path, served):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOP SECRET")
    try:
        (workspace / "link.txt").symlink_to(outside / "secret.txt")
        (workspace / "linkdir").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this host")
    server = served()
    assert _fetch(server, "/link.txt")[0] == 404
    assert _fetch(server, "/linkdir/secret.txt")[0] == 404


def test_server_refuses_sensitive_files_and_what_resolve_tool_path_refuses(workspace, monkeypatch, served):
    from src import tool_execution

    (workspace / "app.js").write_text("ok")
    (workspace / "creds.json").write_text("TOKEN")
    (workspace / "other.txt").write_text("denied by the resolver")
    real = tool_execution._is_sensitive_path
    monkeypatch.setattr(
        tool_execution, "_is_sensitive_path",
        lambda resolved, allow_private=False: resolved.endswith("creds.json") or real(resolved, allow_private),
    )
    original = tool_execution._resolve_tool_path

    def picky(raw, *args, **kwargs):
        if str(raw).endswith("other.txt"):
            raise ValueError("refused")
        return original(raw, *args, **kwargs)

    monkeypatch.setattr(tool_execution, "_resolve_tool_path", picky)
    server = served()
    assert _fetch(server, "/app.js")[0] == 200
    assert _fetch(server, "/creds.json")[0] == 404
    assert _fetch(server, "/other.txt")[0] == 404


def test_server_passes_the_private_grant_through(workspace, monkeypatch, served):
    from src import tool_execution

    (workspace / "a.txt").write_text("x")
    seen = []
    original = tool_execution._resolve_tool_path

    def spy(raw, *args, **kwargs):
        seen.append(kwargs.get("allow_private"))
        return original(raw, *args, **kwargs)

    monkeypatch.setattr(tool_execution, "_resolve_tool_path", spy)
    assert _fetch(served(allow_private=True), "/a.txt")[0] == 200
    assert _fetch(served(allow_private=False), "/a.txt")[0] == 200
    assert seen == [True, False]


def test_server_is_get_head_only_and_size_capped(workspace, monkeypatch, served):
    import urllib.request

    (workspace / "a.txt").write_text("hello")
    server = served()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    head = urllib.request.Request(f"http://127.0.0.1:{server.port}/a.txt", method="HEAD")
    with opener.open(head, timeout=5) as response:
        assert response.status == 200 and response.read() == b""
    post = urllib.request.Request(f"http://127.0.0.1:{server.port}/a.txt", data=b"x", method="POST")
    with pytest.raises(Exception) as err:
        opener.open(post, timeout=5)
    assert getattr(err.value, "code", None) == 405
    monkeypatch.setattr(preview_tools, "MAX_SERVED_BYTES", 3)
    assert _fetch(server, "/a.txt")[0] == 404


def test_root_is_workspace_else_the_files_own_directory(tmp_path, workspace):
    import os

    inside = workspace / "pages" / "a.html"
    inside.parent.mkdir()
    inside.write_text("x")
    assert preview_tools._server_root_for(os.path.realpath(inside)) == os.path.realpath(workspace)
    elsewhere = tmp_path / "vault" / "note.html"
    elsewhere.parent.mkdir()
    elsewhere.write_text("x")
    assert preview_tools._server_root_for(os.path.realpath(elsewhere)) == os.path.realpath(elsewhere.parent)


def test_server_is_shut_down_after_the_render(workspace, monkeypatch):
    (workspace / "a.html").write_text("<p>hi</p>")
    ports = []

    class Proc:
        async def communicate(self):
            return b"", b""

    async def fake_exec(*argv, **kwargs):
        ports.append(int(argv[-1].split(":")[2].split("/")[0]))
        out = next(a for a in argv if a.startswith("--screenshot=")).split("=", 1)[1]
        with open(out, "wb") as handle:
            handle.write(_png(1280 + 64, 800))
        return Proc()

    monkeypatch.setattr(preview_tools, "find_browser", lambda: "chrome")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert _run({"path": "a.html"})["exit_code"] == 0
    import socket

    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", ports[0]), timeout=2).close()
