"""Serve the real static UI with a stubbed API so a headless browser can drive it.

The Agamemnon layout regressions (every Workbench tab drawn at once, a composer
pushed below the fold, columns overflowing at 1024px) are only visible with
the cascade actually applied, so string checks on the stylesheets cannot catch
them. This helper serves ``static/index.html`` and ``static/`` exactly as the
app ships them and answers ``/api/*`` with small, fixed fixtures: enough for
the chat transcript, the Agents fleet and the Workbench run views to render
through their own JavaScript.

Only the standard library is used: :class:`Chromium` drives a system
Chromium over the DevTools protocol on a pipe, so there is no Playwright or
websocket dependency. Tests skip when no Chromium binary is found.
"""
from __future__ import annotations

import json
import mimetypes
import os
import shutil
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
    """What the page sent us, for assertions (e.g. the saved soldier appearance)."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.chat_tidy_requests = 0
        self.repo_activity = (200, 100)
        self.appearance: dict[str, str | None] = {}
        self.run_status = 'running'


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
    if path in ("/api/workbench/activity", "/api/agents/history"):
        if path == "/api/workbench/activity":
            events = list(ACTIVITY['events'])
            if state.run_status != 'running':
                events.append({"seq": 4, "ts": NOW - 20, "kind": "run_finished", "source": "claude_code",
                               "run_id": RUN_ID, "session_id": SESSION_ID, "title": "Migration check finished",
                               "data": {"status": state.run_status}})
            return 200, {"events": events}
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
                if self.command in ("POST", "PUT"):
                    state.posts.append((path, body if isinstance(body, dict) else {}))
                status, payload = _api_response(state, self.command, path, body if isinstance(body, dict) else {})
                self._send(status, json.dumps(payload).encode(), "application/json")
                return
            if path in ("/", "/index.html"):
                html = (STATIC / "index.html").read_text().replace("{{CSP_NONCE}}", "harness")
                self._send(200, html.encode(), "text/html; charset=utf-8")
                return
            # No service worker: it would cache assets across test pages.
            if path.startswith("/static/") and path != "/static/sw.js":
                target = (STATIC / unquote(path[len("/static/"):])).resolve()
                if target.is_file() and STATIC.resolve() in target.parents:
                    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                    if target.suffix in (".js", ".mjs"):
                        ctype = "text/javascript"
                    self._send(200, target.read_bytes(), ctype)
                    return
            self._send(404, b"not found", "text/plain")

        do_GET = do_HEAD = do_POST = do_PATCH = do_PUT = do_DELETE = _handle

    return Handler


class StaticAppServer:
    def __init__(self) -> None:
        self.state = StubState()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
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


def chromium_path() -> str | None:
    for candidate in (os.environ.get("ODYSSEUS_TEST_CHROMIUM"), "/usr/bin/chromium", "/usr/bin/chromium-browser",
                      shutil.which("chromium"), shutil.which("chromium-browser"), shutil.which("google-chrome")):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


# Geometry probe evaluated in the page. Returns plain data so assertions read
# as facts about what a user sees: is it drawn, where, can it be clicked.
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


class BrowserError(RuntimeError):
    pass


class Chromium:
    """Headless Chromium over the DevTools protocol on a pipe (stdlib only).

    ``--remote-debugging-pipe`` reads NUL-terminated JSON commands on fd 3
    and writes responses/events on fd 4, so no websocket client or port is
    needed. Each :meth:`page` gets its own browser context (fresh storage).
    """

    def __init__(self, exe: str) -> None:
        import subprocess
        import tempfile
        self._profile = tempfile.mkdtemp(prefix="ody-cdp-")
        cmd_r, cmd_w = os.pipe()
        res_r, res_w = os.pipe()
        # Park the child's ends above the low fds so the dup2 to 3/4 below
        # never overwrites one end with the other.
        import fcntl
        child_r = fcntl.fcntl(cmd_r, fcntl.F_DUPFD, 10)
        child_w = fcntl.fcntl(res_w, fcntl.F_DUPFD, 10)
        os.close(cmd_r)
        os.close(res_w)

        def wire():
            os.dup2(child_r, 3)
            os.dup2(child_w, 4)

        self.proc = subprocess.Popen(
            [exe, "--headless=new", "--remote-debugging-pipe", "--no-sandbox", "--disable-gpu",
             "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check", "--mute-audio",
             "--disable-background-networking", "--disable-extensions", "--force-color-profile=srgb",
             f"--user-data-dir={self._profile}", "about:blank"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            pass_fds=(3, 4, child_r, child_w), preexec_fn=wire,
        )
        os.close(child_r)
        os.close(child_w)
        self._out = os.fdopen(cmd_w, "wb", buffering=0)
        self._in = os.fdopen(res_r, "rb", buffering=0)
        self._next = 0
        self._lock = threading.Lock()
        self._waiters: dict[int, list] = {}
        self.events: list[dict] = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self.send("Target.setDiscoverTargets", {"discover": False})

    def _read(self) -> None:
        buf = b""
        while True:
            try:
                chunk = self._in.read(65536)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            buf += chunk
            while b"\0" in buf:
                raw, buf = buf.split(b"\0", 1)
                msg = json.loads(raw)
                if "id" in msg and msg["id"] in self._waiters:
                    slot = self._waiters[msg["id"]]
                    slot.append(msg)
                    slot[0].set()
                else:
                    self.events.append(msg)
        for slot in list(self._waiters.values()):
            slot[0].set()

    def send(self, method: str, params: dict | None = None, session: str | None = None, timeout: float = 30) -> dict:
        with self._lock:
            self._next += 1
            mid = self._next
            slot = [threading.Event()]
            self._waiters[mid] = slot
            msg = {"id": mid, "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            self._out.write(json.dumps(msg).encode() + b"\0")
        if not slot[0].wait(timeout):
            raise BrowserError(f"{method} timed out")
        self._waiters.pop(mid, None)
        if len(slot) < 2:
            raise BrowserError(f"browser exited during {method}")
        reply = slot[1]
        if "error" in reply:
            raise BrowserError(f"{method}: {reply['error']}")
        return reply.get("result", {})

    def page(self, width: int, height: int) -> "Page":
        ctx = self.send("Target.createBrowserContext", {"disposeOnDetach": True})["browserContextId"]
        target = self.send("Target.createTarget", {"url": "about:blank", "browserContextId": ctx})["targetId"]
        session = self.send("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        return Page(self, session, ctx, width, height)

    def close(self) -> None:
        try:
            self.send("Browser.close", timeout=5)
        except BrowserError:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        for f in (self._out, self._in):
            try:
                f.close()
            except OSError:
                pass
        shutil.rmtree(self._profile, ignore_errors=True)


class Page:
    def __init__(self, browser: Chromium, session: str, context: str, width: int, height: int) -> None:
        self.b, self.s, self.ctx = browser, session, context
        self.width, self.height = width, height
        for domain in ("Page.enable", "Runtime.enable"):
            self.send(domain)
        # A background headless tab never has focus, so clicks would not
        # move document.activeElement; emulate a focused page.
        self.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        self.send("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height, "deviceScaleFactor": 1, "mobile": width <= 480})

    def send(self, method: str, params: dict | None = None) -> dict:
        return self.b.send(method, params, session=self.s)

    @property
    def errors(self) -> list[str]:
        out = []
        for ev in self.b.events:
            if ev.get("sessionId") == self.s and ev.get("method") == "Runtime.exceptionThrown":
                d = ev["params"]["exceptionDetails"]
                out.append((d.get("exception") or {}).get("description") or d.get("text", ""))
        return out

    def goto(self, url: str, settle: float = 2.0) -> None:
        self.send("Page.navigate", {"url": url})
        self.wait_for("document.readyState === 'complete'", timeout=20)
        time.sleep(settle)

    def eval(self, expression: str):
        r = self.send("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True,
                                           "userGesture": True})
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise BrowserError((d.get("exception") or {}).get("description") or d.get("text"))
        return r["result"].get("value")

    def wait_for(self, expression: str, timeout: float = 8.0) -> bool:
        # Coerced in the page: a DOM node serializes to {}, which is falsy here.
        end = time.time() + timeout
        while True:
            if self.eval(f"(() => {{ try {{ return !!({expression}); }} catch (_) {{ return false; }} }})()"):
                return True
            if time.time() > end:
                raise BrowserError(f"timed out waiting for: {expression}")
            time.sleep(0.1)

    def probe(self, selector: str):
        return self.eval(f"({PROBE_JS})({json.dumps(selector)})")

    def contrast(self, selector: str):
        return self.eval(f"({CONTRAST_JS})({json.dumps(selector)})")

    def click(self, selector: str) -> None:
        """A real pointer click at the element's centre, after scrolling it
        into view. Whatever is drawn on top at that point receives it, as it
        would for a user, so a covered control fails the test."""
        box = self.eval(
            f"(() => {{ const el = document.querySelector({json.dumps(selector)}); if (!el) return null;"
            " el.scrollIntoView({block: 'center', inline: 'nearest'}); const r = el.getBoundingClientRect();"
            " return {x: r.left + r.width / 2, y: r.top + r.height / 2}; })()")
        if not box:
            raise BrowserError(f"no element for {selector}")
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.send("Input.dispatchMouseEvent", {"type": kind, "x": box["x"], "y": box["y"], "button": "left",
                                                   "clickCount": 1 if kind != "mouseMoved" else 0})
        time.sleep(0.25)

    def type(self, text: str) -> None:
        """Insert text at the focused element, as an IME/keyboard would."""
        self.send("Input.insertText", {"text": text})
        time.sleep(0.1)

    def before_load(self, source: str) -> None:
        """Run ``source`` in every new document before its own scripts."""
        self.send("Page.addScriptToEvaluateOnNewDocument", {"source": source})

    def press(self, key: str, code: str | None = None, key_code: int = 0) -> None:
        for kind in ("rawKeyDown", "keyUp"):
            self.send("Input.dispatchKeyEvent", {"type": kind, "key": key, "code": code or key,
                                                 "windowsVirtualKeyCode": key_code})
        time.sleep(0.15)

    def screenshot(self, path) -> None:
        import base64
        data = self.send("Page.captureScreenshot", {"format": "png"})["data"]
        Path(path).write_bytes(base64.b64decode(data))

    def close(self) -> None:
        try:
            self.b.send("Target.disposeBrowserContext", {"browserContextId": self.ctx})
        except BrowserError:
            pass
