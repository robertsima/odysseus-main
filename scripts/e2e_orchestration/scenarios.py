"""Scenario suite for agent orchestration: the cases that broke in real use.

run.sh runs this after driver.py's main flow, against the same app, mock
model (scenario_scripts.py) and git host. Each scenario opens its own chat,
sends one message, and checks what happened through the app's API, the mock's
request log, git and tmux. Every check prints PASS/FAIL; the exit status is 0
only when all passed.

    python scenarios.py --app URL --root ~/e2e [--only name,name]
    python scenarios.py --only shutdown_begin|shutdown_check   (run.sh's restart phase)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from driver import ADMIN_LOADOUT, Report, read_jsonl, stream_chat, wait_for  # noqa: E402

CREDS = {"username": "e2eadmin", "password": "e2e-password-1234"}
MODEL = "e2e-mock"
PUBLISHER = "Publisher"
PUBLISHER_TOOLS = ["manage_agent_worktree", "write_file", "read_file", "ls", "get_workspace"]
ORDER = ["broad_parent", "ssh_remote", "feature_base", "interactive", "stop_worker", "stop_parent",
         "parallel", "publish"]


class Suite:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.root = args.root
        self.clone = os.path.join(self.root, "development", "umni")
        self.data = os.path.join(self.root, "data")
        self.logs = os.path.join(self.root, "logs")
        self.mock_log = os.path.join(self.logs, "mock_requests.jsonl")
        self.rep = Report()
        self.client = httpx.Client(base_url=args.app, timeout=60)
        self.endpoint_id = ""

    # ── setup ────────────────────────────────────────────────────────────
    def login(self) -> bool:
        if not self.rep.check("app is up", wait_for(self.args.app + "/api/auth/status", 120), self.args.app):
            return False
        resp = self.client.post("/api/auth/login", json={**CREDS, "remember": True})
        if not self.rep.check("login", resp.status_code == 200, resp.text[:200]):
            return False
        eps = self.client.get("/api/model-endpoints").json()
        eps = eps.get("endpoints", eps) if isinstance(eps, dict) else eps
        self.endpoint_id = next((e["id"] for e in eps if "e2e-mock" in json.dumps(e)), "")
        return self.rep.check("mock endpoint registered (driver.py ran first)", bool(self.endpoint_id), str(eps)[:300])

    def ensure_publisher(self) -> None:
        settings = self.client.get("/api/auth/settings").json()
        profiles = [p for p in settings.get("agent_profiles") or [] if p.get("name") != PUBLISHER]
        profiles.append({"name": PUBLISHER, "description": "e2e publisher", "tool_access": "selected",
                         "enabled_tools": PUBLISHER_TOOLS, "private_vault_access": False,
                         "max_parallel_workers": 0})
        self.client.post("/api/auth/settings", json={"agent_profiles": profiles})

    def chat(self, name: str, loadout: str = ADMIN_LOADOUT) -> str:
        resp = self.client.post("/api/session", data={"name": f"S {name}", "endpoint_id": self.endpoint_id,
                                                      "model": MODEL})
        sid = resp.json().get("id") or resp.json().get("session_id")
        self.client.post(f"/api/agents/sessions/{sid}/loadout", json={"profile": loadout})
        return sid

    def send(self, sid: str, message: str, *, workspace: Optional[str] = None, name: str = "s") -> List[Dict]:
        form = {"message": message, "session": sid, "selected_model": MODEL,
                "selected_endpoint_id": self.endpoint_id, "mode": "agent", "plan_mode": "false",
                "allow_web_search": "false", "allow_bash": "true"}
        if workspace:
            form["workspace"] = workspace
        return stream_chat(self.client, form, os.path.join(self.logs, f"scenario_{name}.sse"), 120)

    # ── observation ──────────────────────────────────────────────────────
    def rows(self, scenario: str, agent: Optional[str] = None, tag: Optional[str] = None) -> List[Dict]:
        return [r for r in read_jsonl(self.mock_log)
                if r.get("kind") == "scenario" and r.get("scenario") == scenario
                and (agent is None or r.get("agent") == agent) and (tag is None or r.get("tag") == tag)]

    def results(self, scenario: str, tool: str, agent: str = "worker") -> List[str]:
        rows = self.rows(scenario, agent)
        if not rows:
            return []
        return [str(r.get("result") or "") for r in rows[-1].get("results") or [] if r.get("name") == tool]

    def history(self, sid: str) -> List[Dict]:
        resp = self.client.get(f"/api/history/{sid}")
        return resp.json().get("history") or [] if resp.status_code == 200 else []

    def worker_runs(self, scenario: str) -> List[Dict[str, str]]:
        """run_id/session_id of every worker the scenario's chat started, read
        from the manage_agent_loadout results the chat's model was shown."""
        found: Dict[str, Dict[str, str]] = {}
        for row in self.rows(scenario, "chat"):
            for res in row.get("results") or []:
                if res.get("name") != "manage_agent_loadout":
                    continue
                text = str(res.get("result") or "")
                for run in re.finditer(r'\\?"run_id\\?"\s*:\s*\\?"([^"\\]+)', text):
                    child = re.search(r'\\?"session_id\\?"\s*:\s*\\?"([^"\\]+)', text[run.start():])
                    found.setdefault(run.group(1), {"run_id": run.group(1),
                                                    "session_id": child.group(1) if child else ""})
        return list(found.values())

    def run_record(self, run_id: str) -> Dict[str, Any]:
        resp = self.client.get(f"/api/agents/runs/{run_id}/events")
        return resp.json() if resp.status_code == 200 else {}

    def wait(self, what: Callable[[], Any], timeout: float, every: float = 1.0) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = what()
            if value:
                return value
            time.sleep(every)
        return what()

    def wait_run_done(self, run_id: str, timeout: float = 90) -> Dict[str, Any]:
        rec = self.wait(lambda: (lambda r: r if r.get("status") not in (None, "running") else None)(
            self.run_record(run_id)), timeout)
        return rec or self.run_record(run_id)

    def git(self, *args: str, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=cwd or self.clone, capture_output=True, text=True)

    def worker_chat(self, run: Dict[str, str]) -> str:
        if run.get("session_id"):
            return run["session_id"]
        summary = self.run_record(run["run_id"]).get("summary") or {}
        return str(summary.get("target_session") or "")

    def start_worker(self, name: str, message: str, **send_kw) -> Optional[Dict[str, str]]:
        sid = self.chat(name)
        self.send(sid, message, name=name, **send_kw)
        runs = self.wait(lambda: self.worker_runs(name), 20)
        ok = self.rep.check(f"[{name}] worker started", bool(runs), f"chat {sid}")
        if not ok:
            return None
        run = dict(runs[0])
        run["session_id"] = self.worker_chat(run)
        return {"chat": sid, **run}

    # ── scenarios ────────────────────────────────────────────────────────
    def s_broad_parent(self) -> None:
        w = self.start_worker("broad_parent", "Have the Lead Engineer look around umni. (ref E2E-S:broad_parent)",
                              workspace=self.root)
        if not w:
            return
        rec = self.wait_run_done(w["run_id"])
        first = next(iter(self.rows("broad_parent", "worker")), {})
        self.rep.check("[broad_parent] the worker was offered bash", "bash" in (first.get("tools") or []),
                       f"tools: {first.get('tools')}")
        out = " ".join(self.results("broad_parent", "bash"))
        self.rep.check("[broad_parent] bash ran in the repository, not the chat's folder",
                       self.clone in out and "S-NO-DB" in out, out[:400])
        self.rep.check("[broad_parent] worker completed", rec.get("status") == "completed", str(rec.get("status")))

    def s_ssh_remote(self) -> None:
        https = self.git("remote", "get-url", "origin").stdout.strip()
        host = re.sub(r"^https://([^/]+)/.*$", r"\1", https)
        slug = re.sub(r"^https://[^/]+/(.*?)(?:\.git)?$", r"\1", https)
        self.git("remote", "set-url", "origin", f"git@{host}:{slug}.git")
        try:
            w = self.start_worker("ssh_remote", "Have the Lead Engineer fetch umni. (ref E2E-S:ssh_remote)")
            if not w:
                return
            self.wait_run_done(w["run_id"])
            fetch = " ".join(self.results("ssh_remote", "manage_git"))
            self.rep.check("[ssh_remote] manage_git fetch works for a git@ remote",
                           bool(fetch) and '"error"' not in fetch.lower()[:300], fetch[:400])
            bash = " ".join(self.results("ssh_remote", "bash"))
            self.rep.check("[ssh_remote] a failed bash fetch points at manage_git", "manage_git" in bash, bash[:600])
        finally:
            self.git("remote", "set-url", "origin", https)

    def s_feature_base(self) -> None:
        self.git("checkout", "-q", "-B", "feature/next", "origin/main")
        with open(os.path.join(self.clone, "umni", "feature.py"), "w", encoding="utf-8") as fh:
            fh.write("FEATURE = True\n")
        self.git("add", "umni/feature.py")
        self.git("commit", "-qm", "local feature work (not pushed)")
        try:
            w = self.start_worker("feature_base",
                                  "Have the Lead Engineer continue umni from feature/next. (ref E2E-S:feature_base)")
            if not w:
                return
            self.wait_run_done(w["run_id"])
            start = " ".join(self.results("feature_base", "manage_agent_worktree"))
            bash = " ".join(self.results("feature_base", "bash"))
            self.rep.check("[feature_base] worktree starts from a local branch GitHub does not have",
                           "S-HAS-FEATURE" in bash, (bash or start)[:600])
        finally:
            self.git("checkout", "-q", "main")

    def s_interactive(self) -> None:
        t0 = time.monotonic()
        w = self.start_worker("interactive", "Have the Lead Engineer run git in umni. (ref E2E-S:interactive)")
        if not w:
            return
        rec = self.wait_run_done(w["run_id"], timeout=120)
        took = time.monotonic() - t0
        outs = self.results("interactive", "bash")
        joined = "\n".join(outs)
        for marker, what in (("S-LOG-DONE", "git log (a pager in a terminal)"),
                             ("S-COMMIT-RC=", "git commit without -m (an editor)"),
                             ("S-READ-DONE:", "read from stdin")):
            self.rep.check(f"[interactive] {what} returns", marker in joined, joined[-600:])
        self.rep.check("[interactive] worker completed in under a minute",
                       rec.get("status") == "completed" and took < 60, f"{rec.get('status')} in {took:.0f}s")

    def _stop_case(self, name: str, stop_chat: str, w: Dict[str, str]) -> None:
        # Stop once the worker is inside its `sleep 600`.
        self.wait(lambda: self.rows(name, "worker"), 20)
        time.sleep(3)
        resp = self.client.post(f"/api/chat/stop/{stop_chat}")
        body = resp.json() if resp.status_code == 200 else {}
        self.rep.check(f"[{name}] Stop answers stopped", body.get("stopped") is True, f"{resp.status_code} {body}")
        rec = self.wait_run_done(w["run_id"], timeout=30)
        self.rep.check(f"[{name}] the worker run ends as cancelled", rec.get("status") == "cancelled",
                       str(rec.get("status")))
        panes = subprocess.run(["tmux", "ls"], capture_output=True, text=True).stdout
        self.rep.check(f"[{name}] its terminal was closed", w["session_id"][:8] not in panes, panes.strip()[:300])

    def s_stop_worker(self) -> None:
        w = self.start_worker("stop_worker", "Have the Lead Engineer wait in umni. (ref E2E-S:stop_worker)")
        if w:
            self._stop_case("stop_worker", w["session_id"], w)

    def s_stop_parent(self) -> None:
        w = self.start_worker("stop_parent", "Have the Lead Engineer wait in umni. (ref E2E-S:stop_parent)")
        if w:
            self._stop_case("stop_parent", w["chat"], w)

    def s_parallel(self) -> None:
        sid = self.chat("parallel")
        self.send(sid, "Have the Lead Engineer report A and B in umni. (ref E2E-S:parallel)", name="parallel")
        runs = self.wait(lambda: (lambda r: r if len(r) >= 2 else None)(self.worker_runs("parallel")), 20) or []
        self.rep.check("[parallel] two workers started", len(runs) >= 2, str(runs))
        for run in runs:
            self.wait_run_done(run["run_id"])
        followups = self.wait(lambda: [m for m in self.history(sid)
                                       if (m.get("metadata") or {}).get("source") == "worker_followup"], 30)
        time.sleep(4)   # a second follow-up would land by now
        followups = [m for m in self.history(sid) if (m.get("metadata") or {}).get("source") == "worker_followup"]
        text = " ".join(str(m.get("content")) for m in followups)
        self.rep.check("[parallel] exactly one summarising reply", len(followups) == 1, f"{len(followups)} replies")
        self.rep.check("[parallel] it had both results", "S-RESULT-A" in text and "S-RESULT-B" in text, text[:300])

    def s_publish(self) -> None:
        self.ensure_publisher()
        sid = self.chat("publish", loadout=PUBLISHER)
        self.send(sid, "Make the change and ask to publish it. (ref E2E-S:publish)", workspace=self.clone,
                  name="publish")
        rows = self.rows("publish", "chat")
        results = [r for r in (rows[-1].get("results") if rows else []) or []
                   if r.get("name") == "manage_agent_worktree"]
        by_action = {str((r.get("args") or {}).get("action")): str(r.get("result") or "") for r in results}
        commit = by_action.get("commit", "")
        self.rep.check("[publish] a commit in a third-party worktree lands", '"committed": true' in commit,
                       commit[:400] or "no commit result")
        req = by_action.get("request_publish", "")
        proceeds = bool(re.search(r'"request_id"\s*:', req))
        names_fix = "ODYSSEUS_AGENT_GITHUB_TOKEN" in req or "ODYSSEUS_AGENT_PUBLISH_ENABLED" in req
        self.rep.check("[publish] request_publish proceeds or names what is missing", proceeds or names_fix,
                       req[:500] or "no request_publish result")

def shutdown_begin(suite: Suite) -> int:
    """Start a slow reply and hold its stream open (a browser watching it)."""
    if not suite.login():
        return 1
    sid = suite.chat("slow")
    marker = os.path.join(suite.logs, "shutdown_sid")

    def hold() -> None:
        form = {"message": "Write a long answer. (ref E2E-S:slow)", "session": sid, "selected_model": MODEL,
                "selected_endpoint_id": suite.endpoint_id, "mode": "agent", "plan_mode": "false"}
        try:
            with suite.client.stream("POST", "/api/chat_stream", data=form, timeout=120) as resp:
                for line in resp.iter_lines():
                    if "S-SLOW" in line or '"delta"' in line:
                        with open(marker, "w", encoding="utf-8") as fh:
                            fh.write(sid)
        except Exception:
            pass

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    t.join(timeout=150)
    return 0


def shutdown_check(suite: Suite, sid: str) -> int:
    if not suite.login():
        return 1
    log = open(os.path.join(suite.data, "logs", "app.log"), encoding="utf-8", errors="replace").read()
    suite.rep.check("[shutdown] the app stopped the running turn before exiting",
                    "running chat turn(s) for shutdown" in log,
                    next((ln for ln in log.splitlines() if "shutting down" in ln.lower()), "no shutdown line"))
    replies = [m for m in suite.history(sid) if m.get("role") == "assistant"]
    text = " ".join(str(m.get("content")) for m in replies)
    suite.rep.check("[shutdown] the partial reply was saved", "S-SLOW" in text and "part119" not in text,
                    text[:300] or "no assistant message")
    return 0 if suite.rep.ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="http://127.0.0.1:7801")
    ap.add_argument("--root", default=os.path.expanduser("~/e2e"))
    ap.add_argument("--only", default="")
    ap.add_argument("--sid", default="")
    args = ap.parse_args()
    suite = Suite(args)
    if args.only == "shutdown_begin":
        return shutdown_begin(suite)
    if args.only == "shutdown_check":
        return shutdown_check(suite, args.sid)
    print("== Odysseus orchestration scenarios ==", flush=True)
    if not suite.login():
        return 1
    wanted = [s for s in (args.only.split(",") if args.only else ORDER) if s]
    for name in wanted:
        try:
            getattr(suite, f"s_{name}")()
        except Exception as exc:  # noqa: BLE001 - one scenario's crash is its failure, not the suite's
            suite.rep.check(f"[{name}] ran without an exception", False, repr(exc)[:500])
    failed = [s for s in suite.rep.steps if not s["ok"]]
    with open(os.path.join(suite.logs, "scenarios_report.json"), "w", encoding="utf-8") as fh:
        json.dump({"ok": not failed, "steps": suite.rep.steps}, fh, indent=2)
    print(f"\nSCENARIOS: {'PASS' if not failed else 'FAIL'} ({len(suite.rep.steps) - len(failed)}/"
          f"{len(suite.rep.steps)} checks)", flush=True)
    for s in failed:
        print(f"  FAILED {s['step']}: {s['detail'][:300]}", flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
