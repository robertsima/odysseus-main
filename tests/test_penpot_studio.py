import asyncio

import pytest

from src import penpot_studio as ps
from src import penpot_text


ROOT = ps.ROOT_ID
PAGE = "p1"
HELMET = '<svg viewBox="0 0 512 512"><path d="M0 0h512v512H0z" fill="currentColor"/></svg>'
CREDIT = "Game Icons by GameIcons (CC BY 3.0) via iconify.design"


class FakeClient:
    """Stands in for PenpotClient: records update-file changes."""

    def __init__(self, objects=None):
        root = {"id": ROOT, "type": "frame", "shapes": [], "x": 0, "y": 0, "width": 0.01, "height": 0.01}
        self.file = {"id": "f1", "revn": 3, "vern": 0,
                     "data": {"pagesIndex": {PAGE: {"name": "Page 1", "objects": {ROOT: root, **(objects or {})}}}}}
        self.applied = []

    async def get_file(self, file_id):
        return self.file

    async def apply(self, file_id, changes, retries=3):
        self.applied.append(changes)
        return {"revn": 4}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def fake_measure(specs):
        return [penpot_text.Measured([penpot_text.Line(line, len(line) * spec.size * 0.5)
                                      for line in spec.text.split("\n")], exact=True)
                for spec in specs]

    async def fake_icon(icon):
        return HELMET, CREDIT

    monkeypatch.setattr(penpot_text, "measure", fake_measure)
    monkeypatch.setattr(ps, "fetch_icon", fake_icon)


def test_children_are_positioned_relative_to_their_board_and_nested():
    client = FakeClient()
    result = run(ps.build_tree(client, "f1", PAGE, [{
        "type": "frame", "name": "Board", "x": 100, "y": 50, "w": 400, "h": 300,
        "children": [{"type": "rect", "name": "Card", "x": 10, "y": 20, "w": 50, "h": 40, "fill": "#abc"}],
    }]))
    (changes,) = client.applied
    board, card = (c["obj"] for c in changes)
    assert board["parent-id"] == ROOT and board["frame-id"] == ROOT
    assert (card["x"], card["y"]) == (110, 70)
    assert card["parent-id"] == board["id"] and card["frame-id"] == board["id"]
    assert card["fills"] == [{"fill-color": "#aabbcc", "fill-opacity": 1.0}]
    assert result["count"] == 2 and result["applied"] is True


def test_dry_run_writes_nothing():
    client = FakeClient()
    result = run(ps.build_tree(client, "f1", PAGE, [{"type": "rect", "w": 5, "h": 5}], dry_run=True))
    assert client.applied == [] and result["applied"] is False


def test_text_carries_position_data_with_the_measured_width():
    client = FakeClient()
    run(ps.build_tree(client, "f1", PAGE, [
        {"type": "frame", "x": 0, "y": 0, "w": 300, "h": 100, "children": [
            {"type": "text", "text": "HELLO", "x": 20, "y": 10, "size": 20, "line_height": 1.5, "weight": "700",
             "family": "Space Grotesk"},
            {"type": "text", "text": "right", "x": 0, "y": 50, "w": 300, "size": 10, "align": "right"},
        ]}]))
    hello, right = [c["obj"] for c in client.applied[0] if c["obj"]["type"] == "text"]
    (line,) = hello["position-data"]
    assert line["width"] == 50.0 and line["x"] == 20 and line["text"] == "HELLO"
    # y is the BOTTOM of the line box: top 10 + 20px * 1.5
    assert line["y"] == 40.0 and line["height"] == 30.0
    assert line["font-id"] == "gfont-space-grotesk" and line["font-variant-id"] == "700"
    assert hello["grow-type"] == "auto-width" and hello["width"] == 50.0
    # right aligned inside a 300px box: x shifts by box width minus line width
    assert right["position-data"][0]["x"] == pytest.approx(300 - 25)
    assert right["grow-type"] == "auto-height"


def test_icon_becomes_a_path_inside_the_parent_and_reports_attribution():
    client = FakeClient()
    result = run(ps.build_tree(client, "f1", PAGE, [
        {"type": "frame", "x": 10, "y": 10, "w": 200, "h": 200, "children": [
            {"type": "icon", "icon": "game-icons:spartan-helmet", "x": 5, "y": 5, "size": 64, "color": "#111"}]}]))
    path = next(c["obj"] for c in client.applied[0] if c["obj"]["type"] == "path")
    assert (path["x"], path["y"], path["width"], path["height"]) == (15, 15, 64, 64)
    assert path["fills"][0]["fill-color"] == "#111111"
    assert path["content"][0]["command"] == "move-to"
    assert result["attributions"] == [CREDIT]


def test_build_inside_an_existing_frame_uses_its_origin_and_frame_id():
    existing = {"b1": {"id": "b1", "type": "frame", "x": 200, "y": 300, "width": 100, "height": 100, "shapes": []}}
    client = FakeClient(existing)
    run(ps.build_tree(client, "f1", PAGE, [{"type": "rect", "x": 1, "y": 2, "w": 3, "h": 4}], parent_id="b1"))
    rect = client.applied[0][0]["obj"]
    assert (rect["x"], rect["y"]) == (201, 302)
    assert rect["parent-id"] == "b1" and rect["frame-id"] == "b1"


@pytest.mark.parametrize("node,needle", [
    ({"type": "banana"}, "unknown node type"),
    ({"type": "rect", "w": 10}, "needs w and h"),
    ({"type": "rect", "w": 5, "h": 5, "fill": "red"}, "hex"),
    ({"type": "text"}, "no 'text'"),
    ({"type": "icon"}, "needs 'icon'"),
])
def test_bad_nodes_fail_with_an_instruction(node, needle):
    with pytest.raises(ps.PenpotError, match=needle):
        run(ps.build_tree(FakeClient(), "f1", PAGE, [node]))


def test_unknown_page_and_non_container_parent_are_explained():
    with pytest.raises(ps.PenpotError, match="not in this file"):
        run(ps.build_tree(FakeClient(), "f1", "nope", [{"type": "rect", "w": 1, "h": 1}]))
    client = FakeClient({"r1": {"id": "r1", "type": "rect"}})
    with pytest.raises(ps.PenpotError, match="frame or group"):
        run(ps.build_tree(client, "f1", PAGE, [{"type": "rect", "w": 1, "h": 1}], parent_id="r1"))


def test_move_shapes_reparents_under_the_frame():
    objs = {"b1": {"id": "b1", "type": "frame", "x": 0, "y": 0, "width": 9, "height": 9, "shapes": []},
            "s1": {"id": "s1", "type": "rect"}}
    client = FakeClient(objs)
    run(ps.move_shapes(client, "f1", PAGE, ["s1"], "b1"))
    (change,) = client.applied[0]
    assert change == {"type": "mov-objects", "page-id": PAGE, "parent-id": "b1", "frame-id": "b1", "shapes": ["s1"]}
    with pytest.raises(ps.PenpotError, match="not on page"):
        run(ps.move_shapes(client, "f1", PAGE, ["ghost"], "b1"))


def _shape(i, t, x, y, w, h, parent, **extra):
    return {"id": i, "type": t, "name": i, "selrect": {"x": x, "y": y, "width": w, "height": h},
            "parentId": parent, **extra}


def test_layout_check_reports_overflow_overlap_and_unrendered_text():
    objs = {
        "b": _shape("b", "frame", 0, 0, 100, 100, ROOT, shapes=["card", "t1", "t2", "out", "inside"]),
        "card": _shape("card", "rect", 10, 10, 60, 60, "b"),
        "inside": _shape("inside", "rect", 12, 12, 10, 10, "b"),  # contained in card: a label on a card
        "t1": _shape("t1", "text", 40, 40, 50, 20, "b", positionData=[{"x": 0}],
                     content={"children": [{"text": "Hi", "fontFamily": "Work Sans", "fontSize": "12",
                                            "fontWeight": "400", "fills": [{"fillColor": "#fff"}]}]}),
        "t2": _shape("t2", "text", 80, 80, 10, 10, "b"),  # no position data
        "out": _shape("out", "rect", 90, 90, 30, 30, "b"),
    }
    objs[ROOT] = {"id": ROOT, "type": "frame", "shapes": ["b"], "selrect": {"x": 0, "y": 0, "width": 0, "height": 0}}
    file = {"data": {"pagesIndex": {PAGE: {"objects": objs}}}}
    report = ps.describe_page(file, PAGE)
    problems = "\n".join(report["problems"])
    assert "OVERFLOW: rect 'out'" in problems
    assert "OVERLAP:" in problems and "'card'" in problems and "'t1'" in problems
    assert "NO-RENDER: text text 't2'" in problems
    assert "'inside'" not in problems
    assert report["fonts"] == ["Work Sans"]
    t1 = next(s for s in report["shapes"] if s["id"] == "t1")
    assert t1["text"] == "Hi" and t1["font"] == "Work Sans 400 12px"


def test_private_svg_urls_are_refused():
    for url in ("http://localhost/x.svg", "http://192.168.1.5/x.svg", "http://172.20.0.1/x.svg",
                "http://nas.local/x.svg", "file:///etc/passwd", "ftp://example.com/x.svg"):
        with pytest.raises(ps.PenpotError):
            run(ps.fetch_svg_url(url))


def test_config_comes_from_the_environment(monkeypatch):
    monkeypatch.setattr(ps, "_settings_config", lambda: ("", "", ""))
    monkeypatch.setenv("PENPOT_API_URL", "http://192.168.1.122:9001/api/")
    monkeypatch.setenv("PENPOT_ACCESS_TOKEN", "tok")
    cfg = ps.load_config()
    assert cfg.base_url == "http://192.168.1.122:9001" and cfg.token == "tok"


def test_missing_config_says_how_to_fix(monkeypatch):
    monkeypatch.delenv("PENPOT_API_URL", raising=False)
    monkeypatch.delenv("PENPOT_BASE_URL", raising=False)
    monkeypatch.delenv("PENPOT_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(ps, "_saved_penpot_env", lambda: None)
    monkeypatch.setattr(ps, "_settings_config", lambda: ("", "", ""))
    with pytest.raises(ps.PenpotError, match="Settings > Penpot"):
        ps.load_config()


def test_settings_win_over_environment_and_borrowed_row(monkeypatch):
    monkeypatch.setenv("PENPOT_API_URL", "http://env:1")
    monkeypatch.setenv("PENPOT_ACCESS_TOKEN", "env-tok")
    monkeypatch.setattr(ps, "_saved_penpot_env", lambda: ({"PENPOT_API_URL": "http://row:1", "PENPOT_ACCESS_TOKEN": "row"}, "row"))
    monkeypatch.setattr(ps, "_settings_config", lambda: ("http://set:9001/api", "set-tok", "https://design.example/"))
    cfg = ps.load_config()
    assert (cfg.base_url, cfg.token, cfg.source) == ("http://set:9001", "set-tok", "settings")
    assert cfg.public_url == "https://design.example"


def test_borrowed_row_is_last_and_logs_once(monkeypatch, caplog):
    import logging

    for var in ("PENPOT_API_URL", "PENPOT_BASE_URL", "PENPOT_ACCESS_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ps, "_settings_config", lambda: ("", "", ""))
    monkeypatch.setattr(ps, "_borrowed_logged", False)
    monkeypatch.setattr(ps, "_saved_penpot_env", lambda: ({"PENPOT_API_URL": "http://row:1", "PENPOT_ACCESS_TOKEN": "row"}, "MCP server 'Penpot'"))
    with caplog.at_level(logging.INFO, logger="src.penpot_studio"):
        assert ps.load_config().token == "row"
        ps.load_config()
    assert sum("deprecated" in r.message for r in caplog.records) == 1


def test_font_identifiers_follow_penpots_google_font_scheme():
    assert ps.font_fields("Work Sans", "400", False)["font-variant-id"] == "regular"
    assert ps.font_fields("Work Sans", "400", True)["font-variant-id"] == "italic"
    assert ps.font_fields("Archivo Black", "700", True) == {
        "font-id": "gfont-archivo-black", "font-family": "Archivo Black",
        "font-variant-id": "700italic", "font-weight": "700", "font-style": "italic"}


def test_unmeasurable_text_is_flagged_not_hidden(monkeypatch):
    async def estimate_only(specs):
        return [penpot_text.estimate(s) for s in specs]

    monkeypatch.setattr(penpot_text, "measure", estimate_only)
    result = run(ps.build_tree(FakeClient(), "f1", PAGE, [
        {"type": "frame", "w": 100, "h": 50, "children": [{"type": "text", "text": "x", "size": 12}]}]))
    assert result["text_measured_in_browser"] is False
    assert "estimate" in result["warnings"][0]


def test_saved_penpot_mcp_server_supplies_url_and_token(monkeypatch, tmp_path):
    import json

    import core.database as database
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path / 'mcp.db'}")
    database.McpServer.__table__.create(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", Session)
    with Session() as db:
        db.add(database.McpServer(id="x1", name="ntfy", transport="stdio", command="npx",
                                  args=json.dumps(["-y", "ntfy-mcp"]), env="{}", is_enabled=True))
        db.add(database.McpServer(id="c5ec6d7a", name="Penpot", transport="stdio", command="npx",
                                  args=json.dumps(["-y", "@zcubekr/penpot-mcp-server"]),
                                  env=json.dumps({"PENPOT_API_URL": "http://192.168.1.122:9001",
                                                  "PENPOT_ACCESS_TOKEN": "secret"}), is_enabled=True))
        db.commit()
    for var in ("PENPOT_API_URL", "PENPOT_BASE_URL", "PENPOT_ACCESS_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ps, "_settings_config", lambda: ("", "", ""))

    cfg = ps.load_config()

    assert (cfg.base_url, cfg.token) == ("http://192.168.1.122:9001", "secret")
    assert "Penpot" in cfg.source


def test_hostnames_resolving_to_private_addresses_are_refused(monkeypatch):
    table = {"evil.example": "10.0.0.5", "v6.example": "::ffff:127.0.0.1", "ok.example": "93.184.216.34"}

    async def fake_resolve(host, port):
        return [table.get(host, host)]

    monkeypatch.setattr(ps, "_resolve", fake_resolve)
    for url in ("http://evil.example/x.svg", "http://v6.example/x.svg", "http://127.0.0.1/x.svg",
                "http://169.254.169.254/latest", "http://[::1]/x.svg"):
        with pytest.raises(ps.PenpotError, match="private"):
            run(ps._assert_public(url))
    run(ps._assert_public("https://ok.example/x.svg"))


def test_model_style_field_spellings_are_accepted_and_unknown_fields_reported():
    client = FakeClient()
    result = run(ps.build_tree(client, "f1", PAGE, [
        {"type": "frame", "w": 200, "h": 100, "strokeColor": "#000", "strokeWidth": 3, "children": [
            {"type": "text", "text": "Hi", "fontSize": 30, "fontFamily": "Space Grotesk", "fontWeight": 800,
             "textColor": "#ff0000", "letterSpaceing": 2},
        ]}]))
    board, text = (c["obj"] for c in client.applied[0])
    assert board["strokes"][0]["stroke-width"] == 3.0
    leaf = text["position-data"][0]
    assert leaf["font-size"] == "30" and leaf["font-family"] == "Space Grotesk"
    assert leaf["font-weight"] == "800" and leaf["fills"][0]["fill-color"] == "#ff0000"
    assert any("letter_spaceing" in w and "ignored" in w for w in result["warnings"])


def test_thin_multi_word_icon_search_is_broadened_word_by_word(monkeypatch):
    calls = []

    async def fake_once(query, prefix, limit):
        calls.append(query)
        table = {"greek helmet": {"icons": [], "collections": {}},
                 "greek": {"icons": ["game-icons:greek-temple"],
                           "collections": {"game-icons": {"name": "Game Icons", "license": {"title": "CC BY 3.0"}}}},
                 "helmet": {"icons": ["game-icons:helmet", "game-icons:greek-helmet"],
                            "collections": {"game-icons": {"name": "Game Icons", "license": {"title": "CC BY 3.0"}}}}}
        return table[query]

    monkeypatch.setattr(ps, "_search_once", fake_once)
    result = run(ps.search_icons("greek helmet"))
    assert calls == ["greek helmet", "greek", "helmet"]
    assert result["icons"][0] == "game-icons:greek-helmet"  # matches both words: first
    assert "note" in result and result["sets"]["game-icons"]["attribution_required"] is True


def test_missing_icon_error_says_to_search(monkeypatch):
    async def gone(url, **kw):
        raise ps.PenpotError(f"{url} answered HTTP 404")

    monkeypatch.undo()  # use the real fetch_icon, not the autouse fake
    monkeypatch.setattr(ps, "_get", gone)
    with pytest.raises(ps.PenpotError, match="search_icons"):
        run(ps.fetch_icon("game-icons:centurion-helmet"))


# --- render: public URI mismatch and error-page detection (2026-10-01) -------

CONFIG_JS = 'var penpotPublicURI = "http://homelab.nas:9001";\nvar penpotFlags = "x";'


def test_public_uri_is_parsed_from_config_js():
    assert ps.parse_public_uri(CONFIG_JS) == "http://homelab.nas:9001"
    assert ps.parse_public_uri("var penpotPublicURI = 'https://d.example.com/pp/';") == "https://d.example.com"
    assert ps.parse_public_uri("var penpotFlags = 'x';") is None
    assert ps.parse_public_uri('var penpotPublicURI = "not a url";') is None
    assert ps.parse_public_uri("") is None


def _cfg(base="http://192.168.1.122:9001"):
    return ps.PenpotConfig(base, "tok", "test")


def test_resolver_rule_only_when_hosts_differ(monkeypatch):
    monkeypatch.delenv("PENPOT_PUBLIC_URL", raising=False)
    origin, flags = ps.viewer_origin(_cfg(), "http://homelab.nas:9001")
    assert origin == "http://homelab.nas:9001"
    assert flags == ["--host-resolver-rules=MAP homelab.nas:9001 192.168.1.122:9001"]
    assert ps.viewer_origin(_cfg(), "http://192.168.1.122:9001") == ("http://192.168.1.122:9001", [])
    assert ps.viewer_origin(_cfg(), None) == ("http://192.168.1.122:9001", [])
    # same host, different port still needs the rule (port-specific map)
    assert ps.viewer_origin(_cfg(), "http://192.168.1.122:8080")[1]
    url = ps.viewer_url(_cfg(), "f", "p", "b", "s", origin)
    assert url.startswith("http://homelab.nas:9001/#/view?")


def test_public_uri_fetch_is_cached_and_tolerates_failure(monkeypatch):
    import httpx
    ps._public_uri_cache.clear()
    calls = []

    class Resp:
        status_code = 200
        text = CONFIG_JS

    class Http:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            calls.append(url)
            if "down" in url:
                raise httpx.ConnectError("nope")
            return Resp()

    monkeypatch.setattr(ps.httpx, "AsyncClient", Http)
    assert run(ps.fetch_public_uri("http://nas:9001")) == "http://homelab.nas:9001"
    assert run(ps.fetch_public_uri("http://nas:9001")) == "http://homelab.nas:9001"
    assert calls == ["http://nas:9001/js/config.js"]
    assert run(ps.fetch_public_uri("http://down:9001")) is None
    assert "http://down:9001" not in ps._public_uri_cache
    ps._public_uri_cache.clear()


ERROR_DOM = ('<html><body><section class="main_ui_static__exception-layout"><div>Internal Error</div>'
             '<div>Something bad happened.</div></section></body></html>')
GOOD_DOM = '<html><body><div class="main_ui_viewer__viewer-layout"><svg></svg></div></body></html>'


def _render_setup(monkeypatch, tmp_path, dom, public_uri="http://homelab.nas:9001"):
    class RenderClient(FakeClient):
        cfg = _cfg()

        def __init__(self):
            super().__init__({"b1": {"id": "b1", "type": "frame", "x": 0, "y": 0, "width": 100, "height": 100,
                                     "parentId": ROOT, "frameId": ROOT}})
            self.file["data"]["pagesIndex"][PAGE]["objects"][ROOT]["shapes"] = ["b1"]
            self.deleted = []

        async def rpc(self, cmd, params):
            if cmd == "delete-share-link":
                self.deleted.append(params["id"])
                return None
            return {"id": "share1"}

    runs = []

    async def fake_public(base):
        return public_uri

    async def fake_browser(args, timeout):
        runs.append(args)
        for a in args:
            if a.startswith("--screenshot="):
                with open(a.split("=", 1)[1], "wb") as fh:
                    fh.write(b"\x89PNG" + b"0" * 2000)
        return (dom.encode() if "--dump-dom" in args else b""), b""

    monkeypatch.delenv("PENPOT_PUBLIC_URL", raising=False)
    monkeypatch.setattr(ps, "fetch_public_uri", fake_public)
    monkeypatch.setattr(ps, "_run_browser", fake_browser)
    monkeypatch.setattr(ps, "browser_executable", lambda: "chromium")
    return RenderClient(), runs, str(tmp_path / "b.png")


def test_error_page_is_an_error_not_a_screenshot(monkeypatch, tmp_path):
    client, runs, out = _render_setup(monkeypatch, tmp_path, ERROR_DOM)
    with pytest.raises(ps.PenpotError, match="error page.*Internal Error"):
        run(ps.render_board(client, "f1", PAGE, None, out))
    import os
    assert not os.path.exists(out)
    assert client.deleted == ["share1"]


def test_healthy_viewer_returns_the_screenshot_with_public_origin(monkeypatch, tmp_path):
    client, runs, out = _render_setup(monkeypatch, tmp_path, GOOD_DOM)
    result = run(ps.render_board(client, "f1", PAGE, None, out))
    assert result["path"] == out and result["viewer_url"].startswith("http://homelab.nas:9001/#/view")
    assert len(runs) == 2
    for argv in runs:
        assert "--host-resolver-rules=MAP homelab.nas:9001 192.168.1.122:9001" in argv
        assert argv[-1] == result["viewer_url"]


def test_no_resolver_rule_when_public_uri_matches(monkeypatch, tmp_path):
    client, runs, out = _render_setup(monkeypatch, tmp_path, GOOD_DOM, public_uri=None)
    run(ps.render_board(client, "f1", PAGE, None, out))
    assert not any(a.startswith("--host-resolver-rules") for argv in runs for a in argv)


def test_error_words_inside_a_real_viewer_are_not_an_error():
    dom = '<div class="main_ui_viewer__x"><text>Oops! Internal Error</text></div>'
    assert ps.penpot_error_page(dom) is None
    assert ps.penpot_error_page(GOOD_DOM) is None
    assert ps.penpot_error_page("<body>Oops! This page doesn't exist</body>")


def test_penpot_217_error_toast_is_an_error_even_with_viewer_in_scripts():
    # Trimmed from the DOM Penpot 2.17 on the user's NAS served for a viewer that
    # could not load (2026-10-01): an error-level toast, and "viewer" in a script.
    dom = ('<script src="js/viewer.js"></script><div id="app"><aside class=" main_ui_ds_notifications_toast__toast" '
           'role="alert"><div class="main_ui_ds_notifications_shared_notification_pill__level-error">'
           '<svg></svg>Something wrong has happened.</div></div><button aria-label="Close" '
           'class="main_ui_ds_notifications_toast__close-button main_ui_ds_notifications_toast__level-error">'
           '</button></aside></div>')
    assert "Something wrong has happened" in (ps.penpot_error_page(dom) or "")


def test_icon_artwork_returns_svg_licence_and_credit(monkeypatch):
    async def fake_get(url, **kw):
        class R:
            text = HELMET
            def json(self):
                return {"game-icons": {"name": "Game Icons", "author": {"name": "GameIcons", "url": "https://game-icons.net"},
                                       "license": {"title": "CC BY 3.0", "spdx": "CC-BY-3.0", "url": "https://x/l"}}}
        return R()

    monkeypatch.undo()
    monkeypatch.setattr(ps, "_get", fake_get)
    out = run(ps.icon_artwork(["game-icons:spartan-helmet", "bad id"] + [f"a:b{i}" for i in range(8)]))
    first = out["artwork"][0]
    assert first["svg"] == HELMET and first["license"]["spdx"] == "CC-BY-3.0"
    assert first["author"]["name"] == "GameIcons" and first["attribution_required"] is True
    assert "spartan-helmet" in first["attribution"] and "CC BY 3.0" in first["attribution"]
    assert first["source_url"].endswith("/game-icons/spartan-helmet/")
    assert "error" in out["artwork"][1]
    assert len(out["artwork"]) == ps.ICON_ARTWORK_MAX_IDS and out["not_fetched"]
