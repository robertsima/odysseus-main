"""Serve the real static UI with a canned API, and measure it in a browser.

Layout regressions such as every Workbench tab drawn at once, a composer
pushed below the fold, or columns overflowing at 1024 px only show with the
cascade applied, so string checks on the stylesheets cannot catch them.
:class:`StaticAppServer` serves ``static/index.html`` and ``static/`` exactly
as the app ships them and answers ``/api/*`` with small fixed fixtures: enough
for the chat transcript, the Agents fleet (Phalanx) and the Workbench run
views to render through their own JavaScript.

The functions below take a Playwright page and return plain data, so test
assertions read as facts about what a user sees: is it drawn, where, can a
click at its centre reach it.
"""
from __future__ import annotations

import json
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "static"
NOW = time.time()

SESSION_ID = "sess-strategy"
PARENT_ID = "agent-lead"
WORKER_ID = "agent-worker"
OTHER_ID = "agent-scout"
RUN_ID = "run-1"

LONG_REPLY = (
    "Here is the plan for the migration.\n\n"
    + "\n".join(f"{i}. Step {i}: verify the schema, migrate the rows and confirm the counts match." for i in range(1, 25))
    + "\n\n```python\nfor row in rows:\n    migrate(row)  # a deliberately long line that must scroll inside the code block rather than widen the transcript column\n```\n"
)

HISTORY = {
    "history": [
        {"role": "user", "content": "Plan the database migration for the orders service."},
        {"role": "assistant", "content": LONG_REPLY, "model": "anthropic/claude-sonnet-5"},
        {"role": "user", "content": "Now summarise the risks."},
        {"role": "assistant", "content": "The main risk is a long lock on the orders table.", "model": "anthropic/claude-sonnet-5"},
    ],
    "model": "anthropic/claude-sonnet-5",
    "offset": 0, "limit": 50, "total": 4, "has_more_before": False,
}

AGENT_ROWS = [
    {
        "session_id": PARENT_ID, "name": "Lead engineer", "status": "running", "source": "odysseus",
        "model": "anthropic/claude-sonnet-5", "started_at": NOW - 300, "latest": "Reviewing the migration plan",
        "profile": "Lead Engineer", "children": [{"run_id": "child-1", "source": "session", "status": "running", "title": "Worker"}],
        "children_running": 1, "soldier_appearance": None, "config": {},
    },
    {
        "session_id": WORKER_ID, "name": "↳ Scout: map the schema", "status": "running", "source": "session",
        "model": "openai/gpt-5", "started_at": NOW - 120, "latest": "Reading tables", "parent_session": PARENT_ID,
        "parent_name": "Lead engineer", "config": {},
    },
    {
        "session_id": OTHER_ID, "name": "Release reviewer", "status": "finished", "source": "odysseus",
        "model": "google/gemini-3-pro", "started_at": NOW - 4000, "finished_at": NOW - 3600,
        "latest": "Signed off the release notes", "config": {},
    },
]

ACTIVITY = {
    "events": [
        {"seq": 1, "ts": NOW - 60, "kind": "run_started", "source": "claude_code", "run_id": RUN_ID,
         "session_id": SESSION_ID, "title": "Claude Code · migrate orders", "data": {}},
        {"seq": 2, "ts": NOW - 50, "kind": "tool_start", "source": "claude_code", "run_id": RUN_ID,
         "session_id": SESSION_ID, "title": "Read schema.sql", "data": {}},
        {"seq": 3, "ts": NOW - 40, "kind": "tool_result", "source": "claude_code", "run_id": RUN_ID,
         "session_id": SESSION_ID, "title": "Read schema.sql", "data": {"excerpt": "CREATE TABLE orders (...)"}},
    ],
}


class StubState:
    """What the page sent us, for assertions (e.g. the saved soldier appearance),
    and the knobs a test may turn. One server serves a whole test process, so
    the fixture calls :meth:`reset` before every test."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.chat_tidy_requests = 0
        self.repo_activity = (200, 100)
        self.appearance: dict[str, str | None] = {}
        self.run_status = "running"

    def posted(self, path: str) -> list[dict]:
        return [body for p, body in list(self.posts) if p == path]


def _api_response(state: StubState, method: str, path: str, body: dict) -> tuple[int, object]:
    if method == "POST" and path == "/api/chats/tidy":
        state.chat_tidy_requests += 1
        return 200, {"status": "ok", "updated": 0, "folders": []}
    if method == "PUT" and path == "/api/workbench/repo/file":
        return 200, {"file": body.get("file"), "version": "b" * 64}
    if method == "POST" and path.startswith("/api/agents/sessions/") and path.endswith("/appearance"):
        sid = unquote(path.split("/")[4])
        state.appearance[sid] = body.get("appearance")
        return 200, {"ok": True, "appearance": body.get("appearance")}
    if method != "GET":
        return 200, {"ok": True}
    if path == "/api/auth/status":
        return 200, {"authenticated": True, "username": "tester", "is_admin": True}
    if path == "/api/auth/settings":
        return 200, {"workbench_enabled": True, "workbench_auto_open": False}
    if path == "/api/sessions":
        return 200, [{"id": SESSION_ID, "name": "Orders migration", "model": "anthropic/claude-sonnet-5",
                      "updated_at": "2026-10-01T10:00:00Z", "message_count": 4}]
    if path.startswith("/api/history/"):
        return 200, HISTORY
    if path.startswith("/api/agents/sessions/") and path.endswith("/checklist"):
        return 200, {"plan": "- [x] Inspect migration tables\n- [ ] Migrate orders in batches\n- [ ] Verify counts and rollback"}
    if path == "/api/agents/overview":
        rows = [dict(row, soldier_appearance=state.appearance.get(row["session_id"], row.get("soldier_appearance")))
                for row in AGENT_ROWS]
        return 200, {"rows": rows, "totals": {"running": 2, "finished_24h": 1}, "profiles": [], "chats": []}
    if path == "/api/agents/approvals":
        return 200, {"approvals": []}
    if path == "/api/workbench/activity":
        events = list(ACTIVITY["events"])
        if state.run_status != "running":
            events.append({"seq": 4, "ts": NOW - 20, "kind": "run_finished", "source": "claude_code",
                           "run_id": RUN_ID, "session_id": SESSION_ID, "title": "Migration check finished",
                           "data": {"status": state.run_status}})
        return 200, {"events": events}
    if path == "/api/agents/history":
        return 200, {"chats": [], "total": 0}
    if path.startswith("/api/agents/runs/"):
        return 200, {"events": []}
    if path == "/api/workbench/runs":
        return 200, {"runs": [{"run_id": RUN_ID, "session_id": SESSION_ID, "source": "claude_code",
                                "status": state.run_status, "title": "Claude Code · migrate orders",
                                "started_at": NOW - 60, "detail": "Checking migration safety"}]}
    if path == "/api/workbench/repo/roots":
        return 200, {"roots": ["/repo"], "workspace": "/repo/workbench",
                     "repositories": [{"path": "/repo/workbench", "branch": "agent/review", "kind": "worktree", "activity_at": state.repo_activity[0]},
                                      {"path": "/repo/main", "branch": "dev", "kind": "checkout", "activity_at": state.repo_activity[1]}]}
    if path == "/api/workbench/repo/changes":
        return 200, {"files": [{"path": "src/orders.py", "status": "modified", "additions": 2, "deletions": 1},
                               {"path": "tests/test_orders.py", "status": "added", "additions": 18, "deletions": 0}]}
    if path == "/api/workbench/repo/files":
        return 200, {"files": ["src/orders.py", "src/clean.py", "tests/test_orders.py"], "truncated": False}
    if path == "/api/workbench/repo/diff":
        return 200, {"diff": "diff --git a/src/orders.py b/src/orders.py\n--- a/src/orders.py\n+++ b/src/orders.py\n@@ -1,2 +1,3 @@\n-def migrate():\n+def migrate(dry_run=False):\n+    assert dry_run or ready()\n     pass\n"}
    if path == "/api/workbench/repo/file":
        return 200, {"file": "src/orders.py", "ref": "worktree", "content": "def migrate(dry_run=False):\n    assert dry_run or ready()\n    pass\n", "version": "a" * 64, "truncated": False}
    if path == "/api/workbench/repo/commits":
        return 200, {"commits": [{"sha": "cafebabecafebabecafebabecafebabecafebabe", "subject": "Validate migration before rollout", "author": "Alex"}]}
    if path == "/api/workbench/prs/config":
        return 200, {"configured": False, "reason": "No GitHub token in this test"}
    if path == "/api/notes":
        return 200, {"notes": []}
    if path in ("/api/models", "/api/presets/templates", "/api/chat/runs"):
        return 200, []
    return 200, {}


_TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
          ".json": "application/json", ".png": "image/png", ".woff2": "font/woff2", ".html": "text/html"}


def make_handler(state: StubState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep pytest output clean
            pass

        def _send(self, status: int, payload: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def _handle(self) -> None:
            path = urlsplit(self.path).path
            if path.startswith("/api/"):
                # Event streams: 204 tells EventSource to stop reconnecting.
                if path.endswith("/stream"):
                    self._send(204, b"", "text/plain")
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                if self.command in ("POST", "PUT"):
                    state.posts.append((path, body))
                status, payload = _api_response(state, self.command, path, body)
                self._send(status, json.dumps(payload).encode(), "application/json")
                return
            if path in ("/", "/index.html"):
                html = (STATIC / "index.html").read_text(encoding="utf-8").replace("{{CSP_NONCE}}", "harness")
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                return
            # No service worker: it would cache assets across test pages.
            if path.startswith("/static/") and path != "/static/sw.js":
                target = (STATIC / unquote(path[len("/static/"):])).resolve()
                if target.is_file() and STATIC.resolve() in target.parents:
                    # Fixed types first: Windows reads guess_type from the registry.
                    ctype = (_TYPES.get(target.suffix)
                             or mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                    self._send(200, target.read_bytes(), ctype)
                    return
            self._send(404, b"not found", "text/plain")

        do_GET = do_HEAD = do_POST = do_PATCH = do_PUT = do_DELETE = _handle

    return Handler


class StaticAppServer:
    def __init__(self) -> None:
        self.state = StubState()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self) -> "StaticAppServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# ── Measuring a page ────────────────────────────────────────────────────────

# Geometry probe evaluated in the page. ``hit`` is whether a click at the
# element's centre (capped 20 px down from its top) lands on it.
PROBE_JS = r"""
(selector) => {
  const el = document.querySelector(selector);
  if (!el) return null;
  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  let visible = cs.display !== 'none' && cs.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  for (let p = el.parentElement; visible && p; p = p.parentElement) {
    const ps = getComputedStyle(p);
    if (ps.display === 'none' || ps.visibility === 'hidden') visible = false;
  }
  let hit = null;
  if (visible) {
    const x = Math.min(Math.max(r.left + r.width / 2, 0), innerWidth - 1);
    const y = Math.min(Math.max(r.top + Math.min(r.height / 2, 20), 0), innerHeight - 1);
    const top = document.elementFromPoint(x, y);
    hit = !!top && (top === el || el.contains(top));
  }
  return {left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height,
          display: cs.display, position: cs.position, visible, hit,
          color: cs.color, background: cs.backgroundColor, fontSize: parseFloat(cs.fontSize),
          overflowY: cs.overflowY, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight};
}
"""

# WCAG contrast of an element's text against the first opaque background
# behind it (text alpha composited over that background).
CONTRAST_JS = r"""
(selector) => {
  const el = document.querySelector(selector);
  if (!el) return null;
  const parse = (c) => {
    let m = c.match(/rgba?\(([^)]+)\)/);
    if (m) { const p = m[1].split(/[\s,/]+/).filter(Boolean).map(Number); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; }
    m = c.match(/color\(srgb ([^)]+)\)/);
    if (m) { const p = m[1].split(/[\s/]+/).filter(Boolean).map(Number); return [p[0] * 255, p[1] * 255, p[2] * 255, p.length > 3 ? p[3] : 1]; }
    return [0, 0, 0, 1];
  };
  const fg = parse(getComputedStyle(el).color);
  let bg = [255, 255, 255, 1];
  for (let n = el; n; n = n.parentElement) {
    const b = parse(getComputedStyle(n).backgroundColor);
    if (b[3] > 0.99) { bg = b; break; }
  }
  const text = [0, 1, 2].map((i) => fg[i] * fg[3] + bg[i] * (1 - fg[3]));
  const lum = (c) => { const v = c.map((x) => { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); }); return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]; };
  const a = lum(text), b = lum(bg);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}
"""

# Resolves once no finite CSS transition or animation is running and, when a
# selector is given, that element's box has held still for three frames
# (smooth scrolling and slide-ins are not animations, so they need the box).
SETTLE_JS = r"""
async ([selector, timeout]) => {
  const frame = () => new Promise((resolve) => requestAnimationFrame(() => resolve()));
  const moving = () => document.getAnimations().some((a) => a.playState === 'running'
      && a.effect && a.effect.getComputedTiming().endTime !== Infinity);
  const box = () => {
    const el = selector && document.querySelector(selector);
    if (!el) return '';
    const r = el.getBoundingClientRect();
    return [r.left, r.top, r.width, r.height].map((v) => Math.round(v)).join(',');
  };
  const end = performance.now() + timeout;
  let last = null, still = 0;
  while (performance.now() < end) {
    await frame();
    const now = box();
    still = now === last ? still + 1 : 0;
    last = now;
    if (still >= 3 && !moving()) return true;
  }
  return false;
}
"""


def expect(target, message: str | None = None):
    """Playwright's retrying ``expect``, imported on first use so test modules
    still import (and deselect) where Playwright is not installed."""
    from playwright.sync_api import expect as _expect
    return _expect(target, message)


def probe(page, selector: str, *, wait: bool = True) -> dict | None:
    """Box, paint and hit-test facts for ``selector``; ``None`` when absent.

    Waits for the element to exist unless ``wait`` is false: parts of the UI
    re-render from polled data, so a fixed moment may fall between renders.
    """
    if wait:
        page.wait_for_selector(selector, state="attached")
    return page.evaluate(PROBE_JS, selector)


def contrast(page, selector: str) -> float | None:
    page.wait_for_selector(selector, state="attached")
    return page.evaluate(CONTRAST_JS, selector)


def settle(page, selector: str | None = None, timeout_ms: int = 5000) -> None:
    """Wait for CSS transitions to finish and ``selector``'s box to stop moving."""
    assert page.evaluate(SETTLE_JS, [selector, timeout_ms]), f"page did not settle ({selector})"


def width_of(page) -> int:
    return page.viewport_size["width"]


def assert_usable(page, selector: str) -> dict:
    """Drawn, inside the viewport, and the top element at its centre."""
    box = probe(page, selector)
    assert box, f"{selector} missing"
    assert box["visible"], f"{selector} is not drawn: {box}"
    assert box["left"] >= -1 and box["right"] <= width_of(page) + 1, f"{selector} off-screen horizontally: {box}"
    assert box["hit"], f"{selector} is covered by another element: {box}"
    return box


def assert_no_sideways_scroll(page, scroller: str | None = None) -> None:
    widths = page.evaluate(
        "(sel) => { const s = sel ? document.querySelector(sel) : null;"
        " return [document.documentElement.scrollWidth, innerWidth, s ? s.scrollWidth : 0, s ? s.clientWidth : 0]; }",
        scroller)
    assert widths[0] <= widths[1], f"page scrolls sideways: {widths}"
    if scroller:
        assert widths[2] <= widths[3] + 1, f"{scroller} scrolls sideways: {widths}"


def set_select(page, selector: str, value: str) -> None:
    """Choose an option the way a user does: the value changes and ``change`` fires."""
    page.evaluate(
        "([sel, value]) => { const s = document.querySelector(sel); s.value = value;"
        " s.dispatchEvent(new Event('change', {bubbles: true})); }", [selector, value])


def set_checkbox(page, selector: str, checked: bool) -> None:
    page.evaluate(
        "([sel, checked]) => { const t = document.querySelector(sel); t.checked = checked;"
        " t.dispatchEvent(new Event('change', {bubbles: true})); }", [selector, checked])


# ── Driving the app ─────────────────────────────────────────────────────────

def visible_panels(page) -> list[str]:
    """The Workbench tab panels that are drawn."""
    return page.evaluate(
        "[...document.querySelectorAll('#workbench-modal [data-wb-panel]')]"
        ".filter(p => getComputedStyle(p).display !== 'none' && p.getBoundingClientRect().height > 0)"
        ".map(p => p.dataset.wbPanel)")


def open_workbench(page, *, activity: bool = True) -> None:
    page.evaluate("window.workbenchModule.open()")
    page.wait_for_function("!document.getElementById('workbench-modal').classList.contains('hidden')")
    if activity:
        page.click("#wb-tab-activity")
        page.wait_for_function("document.getElementById('wb-tab-activity').classList.contains('active')")
    settle(page, ".workbench-modal-content")


def open_agents(page) -> None:
    page.evaluate("window.agentsDashboard.open()")
    page.wait_for_function("document.querySelectorAll('#agents-dashboard .ag-card').length === 3")
    settle(page, ".agents-modal-content")


def select_agent(page, sid: str) -> None:
    page.click(f'.ag-card-select[data-sid="{sid}"]')
    page.wait_for_function("(sid) => document.querySelector('#ag-detail')?.dataset.sessionId === sid", arg=sid)


def drag(page, start: tuple[float, float], end: tuple[float, float]) -> None:
    """Press at ``start``, move to ``end`` and release, as a user drags."""
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(*end)
    page.mouse.up()


def dock_workbench(page, side: str) -> None:
    """Attach the Workbench to the right with its button, or to the left with
    the title-bar snap gesture (there is no left-dock button)."""
    if side == "right":
        page.click("#wb-dock-right")
    else:
        header = probe(page, "#workbench-modal .modal-header h4")
        edge = page.evaluate("document.querySelector('#sidebar').getBoundingClientRect().right")
        y = header["top"] + min(header["bottom"] - header["top"], 24) / 2
        drag(page, (header["left"] + 25, y), (edge + 2, y))
    page.wait_for_function("(side) => document.getElementById('workbench-modal').classList.contains(`modal-${side}-docked`)",
                           arg=side)
    settle(page, ".workbench-modal-content")


def resize_workbench_dock(page, width: int, side: str = "right") -> None:
    """Drag the dock's own seam, not a test-only inline width."""
    page.wait_for_function(
        "(side) => getComputedStyle(document.querySelector(`.edge-dock-resize-handle-${side}`)).display !== 'none'",
        arg=side)
    grip = probe(page, f".edge-dock-resize-handle-{side}")
    y = min(page.viewport_size["height"] // 2, 350)
    if side == "left":
        inner = page.evaluate(
            "Math.max(document.querySelector('#sidebar').classList.contains('hidden') ? 0"
            " : document.querySelector('#sidebar').getBoundingClientRect().right,"
            " document.querySelector('#icon-rail').getBoundingClientRect().right)")
        target = inner + width
    else:
        target = width_of(page) - width
    drag(page, (grip["left"] + 5, y), (target, y))
    page.wait_for_function(
        "(w) => Math.abs(document.querySelector('.workbench-modal-content').getBoundingClientRect().width - w) < 15",
        arg=width)
    settle(page, ".workbench-modal-content")


def assert_inside_parent(page, selectors) -> None:
    """Each control stays inside its own panel, not merely inside the viewport."""
    for selector, parent in selectors:
        page.wait_for_selector(f"{parent} {selector}", state="attached")
        result = page.evaluate(
            """([target, parent]) => { const el = document.querySelector(target); el.scrollIntoView({block: 'center'});
              const p = el.closest(parent), a = el.getBoundingClientRect(), b = p.getBoundingClientRect();
              return {left: a.left, right: a.right, parentLeft: b.left, parentRight: b.right, width: a.width,
                      visible: getComputedStyle(el).display !== 'none'}; }""",
            [f"{parent} {selector}", parent])
        assert (result["visible"] and result["width"] > 0 and result["left"] >= result["parentLeft"] - 2
                and result["right"] <= result["parentRight"] + 2), (selector, result)


def type_into_editor(page, text: str) -> None:
    """Append to the Workbench editor's draft as typing would."""
    page.evaluate("(text) => { const t = document.querySelector('#wb-editor-text'); t.value += text;"
                  " t.dispatchEvent(new Event('input', {bubbles: true})); }", text)


def editor_value(page) -> str | None:
    return page.evaluate("document.querySelector('#wb-editor-text')?.value ?? null")
