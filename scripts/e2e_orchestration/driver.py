"""Drive the orchestration flow through the app's real HTTP API and check it.

run.sh starts the mock model and the app, then runs this. It does what a user
does in the browser -- first-run setup, log in, register a model endpoint,
save two loadouts, open a chat in agent mode under the admin loadout, send one
message through /api/chat_stream -- then waits for the worker to finish and
for the admin chat's follow-up, and checks the result on disk and in the logs.

Each check prints PASS/FAIL; the first failed step is named at the end and
the exit status is 0 only when every check passed. A JSON report is written
next to the logs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

import httpx

ADMIN_LOADOUT = "Odysseus Admin"
WORKER_LOADOUT = "Lead Engineer"
WORKER_TOOLS = ["manage_git", "manage_agent_worktree", "read_file", "write_file", "edit_file",
                "apply_patch", "ls", "glob", "grep", "bash", "get_workspace"]
ADMIN_TOOLS = ["manage_agent_loadout", "read_file", "ls", "get_workspace"]
USER_MESSAGE = ("Have the Lead Engineer add double(x) with a test to Umni in an isolated "
                "worktree from origin/main, run the tests, and tell me what it reports. (ref E2E-ADMIN)")


class Report:
    def __init__(self) -> None:
        self.steps: List[Dict[str, Any]] = []
        self.started = time.monotonic()

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.steps.append({"step": name, "ok": bool(ok), "detail": detail,
                           "t": round(time.monotonic() - self.started, 1)})
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}" + (f" -- {detail}" if detail else ""), flush=True)
        return ok

    def note(self, text: str) -> None:
        print(f"       {text}", flush=True)

    @property
    def ok(self) -> bool:
        return all(s["ok"] for s in self.steps)


def git(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return rows


def wait_for(url: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=3).status_code < 500:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return False


def stream_chat(client: httpx.Client, data: Dict[str, str], raw_path: str, timeout: float) -> List[Dict[str, Any]]:
    """POST /api/chat_stream the way static/js/chat.js does and collect its SSE events."""
    events: List[Dict[str, Any]] = []
    with open(raw_path, "w", encoding="utf-8") as raw, \
            client.stream("POST", "/api/chat_stream", data=data, timeout=timeout) as resp:
        if resp.status_code != 200:
            body = resp.read().decode("utf-8", "replace")
            raw.write(body)
            raise RuntimeError(f"/api/chat_stream returned {resp.status_code}: {body[:500]}")
        for line in resp.iter_lines():
            raw.write(line + "\n")
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                events.append(json.loads(payload))
            except ValueError:
                events.append({"_raw": payload})
    return events


def history(client: httpx.Client, sid: str) -> List[Dict[str, Any]]:
    resp = client.get(f"/api/history/{sid}")
    resp.raise_for_status()
    return resp.json().get("history") or []


def find_start_result(client: httpx.Client, sid: str, events: List[Dict[str, Any]],
                      mock_rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The manage_agent_loadout start result: run_id and the worker chat id."""
    texts: List[str] = []
    for msg in history(client, sid):
        for ev in (msg.get("metadata") or {}).get("tool_events") or []:
            if isinstance(ev, dict) and ev.get("tool") == "manage_agent_loadout":
                texts.append(str(ev.get("output") or ev.get("result") or ""))
    texts += [json.dumps(e) for e in events if "manage_agent_loadout" in json.dumps(e)]
    texts += [str((r.get("outcome") or {}).get("result") or "") for r in mock_rows
              if r.get("kind") == "admin" and (r.get("outcome") or {}).get("prev") == "admin_start"]
    for text in texts:
        run = re.search(r'\\?"run_id\\?"\s*:\s*\\?"([^"\\]+)', text)
        child = re.search(r'\\?"session_id\\?"\s*:\s*\\?"([^"\\]+)', text)
        if run:
            return {"run_id": run.group(1), "session_id": child.group(1) if child else None, "text": text}
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="http://127.0.0.1:7801")
    ap.add_argument("--mock", default="http://127.0.0.1:7811/v1")
    ap.add_argument("--model", default="e2e-mock")
    ap.add_argument("--root", default=os.path.expanduser("~/e2e"))
    ap.add_argument("--variant", default=os.environ.get("E2E_VARIANT", "main"))
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()

    root = args.root
    data_dir = os.path.join(root, "data")
    dev_root = os.path.join(root, "development")
    clone = os.path.join(dev_root, "umni")
    logs = os.path.join(root, "logs")
    mock_log = os.path.join(logs, "mock_requests.jsonl")
    app_log = os.path.join(data_dir, "logs", "app.log")
    report_path = os.path.join(logs, "report.json")
    rep = Report()
    print(f"== Odysseus orchestration e2e (variant: {args.variant}) ==", flush=True)

    def finish() -> int:
        failed = next((s for s in rep.steps if not s["ok"]), None)
        total = round(time.monotonic() - rep.started, 1)
        print("", flush=True)
        if failed:
            print(f"RESULT: FAIL at step '{failed['step']}' after {total}s -- {failed['detail']}", flush=True)
        else:
            print(f"RESULT: PASS ({len(rep.steps)} checks) in {total}s", flush=True)
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump({"variant": args.variant, "ok": rep.ok, "seconds": total, "steps": rep.steps},
                      fh, indent=2)
        return 0 if rep.ok else 1

    # ── setup through the API ────────────────────────────────────────────
    if not rep.check("app is up", wait_for(args.app + "/api/auth/status", 120), args.app):
        return finish()
    client = httpx.Client(base_url=args.app, timeout=60)
    creds = {"username": "e2eadmin", "password": "e2e-password-1234"}
    resp = client.post("/api/auth/setup", json=creds)
    rep.check("first-run admin setup", resp.status_code == 200 or "Already configured" in resp.text,
              f"{resp.status_code} {resp.text[:200]}")
    resp = client.post("/api/auth/login", json={**creds, "remember": True})
    if not rep.check("login", resp.status_code == 200 and resp.json().get("ok"), f"{resp.status_code} {resp.text[:200]}"):
        return finish()

    resp = client.post("/api/model-endpoints", data={
        "name": "e2e-mock", "base_url": args.mock, "skip_probe": "true", "supports_tools": "true",
        "pinned_models": json.dumps([args.model]),
    })
    endpoint_id = resp.json().get("id") if resp.status_code == 200 else None
    if not rep.check("register mock model endpoint", bool(endpoint_id), f"{resp.status_code} {resp.text[:300]}"):
        return finish()

    profiles = [
        {"name": ADMIN_LOADOUT, "description": "e2e admin chat", "tool_access": "selected",
         "enabled_tools": ADMIN_TOOLS, "delegation_policy": "explicit", "max_parallel_workers": 2,
         "private_vault_access": False},
        {"name": WORKER_LOADOUT, "description": "e2e worker", "tool_access": "selected",
         "enabled_tools": WORKER_TOOLS, "private_vault_access": False, "max_parallel_workers": 0,
         # Empty: the worker inherits the admin chat's model. E2E_WORKER_MODEL
         # pins it on the loadout instead, the way most saved loadouts are.
         "model": os.environ.get("E2E_WORKER_MODEL", "")},
    ]
    settings: Dict[str, Any] = {"claude_code_repository_roots": [dev_root], "agent_profiles": profiles}
    if os.environ.get("E2E_APPROVAL_MODE"):
        settings["agent_approval_mode"] = os.environ["E2E_APPROVAL_MODE"]
    resp = client.post("/api/auth/settings", json=settings)
    saved = resp.json() if resp.status_code == 200 else {}
    names = [p.get("name") for p in saved.get("agent_profiles") or []]
    if not rep.check("save settings (repository root + two loadouts)",
                     resp.status_code == 200 and WORKER_LOADOUT in names and ADMIN_LOADOUT in names,
                     f"{resp.status_code} roots={saved.get('claude_code_repository_roots')} loadouts={names}"
                     if resp.status_code == 200 else resp.text[:300]):
        return finish()
    worker_saved = next(p for p in saved["agent_profiles"] if p["name"] == WORKER_LOADOUT)
    rep.note(f"stored {WORKER_LOADOUT}: tool_access={worker_saved.get('tool_access')} "
             f"enabled={worker_saved.get('enabled_tools')} private_vault_access="
             f"{worker_saved.get('private_vault_access')}")

    resp = client.post("/api/session", data={"name": "E2E Admin", "endpoint_id": endpoint_id,
                                             "model": args.model})
    sid = (resp.json().get("id") or resp.json().get("session_id")) if resp.status_code == 200 else None
    if not rep.check("create admin chat", bool(sid), f"{resp.status_code} {resp.text[:300]}"):
        return finish()
    resp = client.post(f"/api/agents/sessions/{sid}/loadout", json={"profile": ADMIN_LOADOUT})
    if not rep.check(f"apply loadout '{ADMIN_LOADOUT}' to the chat",
                     resp.status_code == 200 and resp.json().get("agent_profile") == ADMIN_LOADOUT,
                     f"{resp.status_code} {resp.text[:200]}"):
        return finish()

    # ── the user's message ───────────────────────────────────────────────
    form = {"message": USER_MESSAGE, "session": sid, "selected_model": args.model,
            "selected_endpoint_id": endpoint_id, "mode": "agent", "plan_mode": "false",
            "allow_web_search": "false", "allow_bash": "true"}
    try:
        events = stream_chat(client, form, os.path.join(logs, "admin_stream.sse"), args.timeout)
        stream_error = ""
    except Exception as exc:  # noqa: BLE001 - reported as the failed step
        events, stream_error = [], str(exc)
    if not rep.check("admin chat turn streams (POST /api/chat_stream)", not stream_error,
                     stream_error or f"{len(events)} events"):
        return finish()
    stream_errors = [e for e in events if isinstance(e, dict) and (e.get("type") == "error" or e.get("error"))]
    if stream_errors:
        rep.note(f"stream error events: {json.dumps(stream_errors)[:600]}")

    mock_rows = read_jsonl(mock_log)
    admin_rows = [r for r in mock_rows if r.get("kind") == "admin"]
    rep.check("admin model asked to start the worker (manage_agent_loadout start)",
              any(r.get("step") == "admin_start" for r in admin_rows),
              f"admin steps: {[r.get('step') for r in admin_rows]}")
    ack = next((r for r in admin_rows if (r.get("outcome") or {}).get("prev") == "admin_start"), None)
    started = find_start_result(client, sid, events, mock_rows)
    if not rep.check("manage_agent_loadout start succeeded", bool(ack and ack["outcome"].get("ok") and started),
                     (started or {}).get("text", "")[:400] if started else
                     ("tool result: " + str((ack or {}).get("outcome", {}).get("result"))[:1200])):
        return finish()
    run_id, worker_sid = started["run_id"], started["session_id"]
    rep.note(f"worker run {run_id}, worker chat {worker_sid}")

    # ── wait for the worker and the hand-back ────────────────────────────
    deadline = time.monotonic() + args.timeout
    run: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/api/agents/runs/{run_id}/events")
        if resp.status_code == 200:
            run = resp.json()
            if run.get("status") not in ("running", None):
                break
        time.sleep(2)
    status = run.get("status")
    worker_sid = worker_sid or (run.get("summary") or {}).get("target_session") or run.get("session_id")
    rep.note(f"worker chat {worker_sid}; run record: status={status} "
             f"summary={json.dumps(run.get('summary') or {})[:300]}")
    worker_rows = [r for r in read_jsonl(mock_log) if r.get("kind") == "worker"]
    rep.note("worker steps (mock): " + " -> ".join(str(r.get("step")) for r in worker_rows))
    if not rep.check("worker run completed", status == "completed",
                     f"status={status!r} error={(run.get('summary') or {}).get('error') or run.get('error')}"):
        # Say what the worker saw last before giving up.
        if worker_rows:
            rep.note("last worker request: " + json.dumps(worker_rows[-1])[:1500])
        return finish()

    # Each scripted worker step, as the tool result the worker model got back.
    outcomes = {(r.get("outcome") or {}).get("prev"): r["outcome"] for r in worker_rows if r.get("outcome")}
    for step, label in (("fetch", "manage_git fetch (origin)"),
                        ("worktree_start", "manage_agent_worktree start (base origin/main)"),
                        ("edit_core", "edit_file umni/core.py in the worktree"),
                        ("write_test", "write_file tests/test_double.py in the worktree"),
                        ("pytest", "bash: python -m pytest -q"
                         + (" in the worktree" if args.variant == "main" else " in the workspace clone (workaround)")),
                        ("diff", "manage_agent_worktree diff")):
        out = outcomes.get(step)
        if out is None:
            rep.check(f"worker step: {label}", False, "the worker never got this step's result")
            continue
        detail = str(out.get("result") or "")[:700].replace("\n", " | ")
        ok = bool(out.get("ok"))
        if step == "pytest":
            text = str(out.get("result") or "")
            ok = ok and bool(re.search(r"\d+ passed", text)) and not re.search(r"\d+ (failed|error)", text)
        rep.check(f"worker step: {label}", ok, detail)
    final_worker = next((r for r in reversed(worker_rows) if r.get("step") == "worker_final"), None)
    rep.check("worker wrote its final report", bool(final_worker),
              (final_worker or {}).get("reply", {}).get("text", "")[:400].replace("\n", " | "))

    # ── on disk: worktree, branch, base, files ───────────────────────────
    wt = (outcomes.get("worktree_start") or {}).get("result") or ""
    match = re.search(r'"path"\s*:\s*"([^"]+e2e-double)"', wt)
    wt_path = match.group(1) if match else ""
    wt_root = os.path.join(data_dir, "agent_worktrees")
    rep.check("worktree is under the agent worktree root", bool(wt_path) and wt_path.startswith(wt_root + "/")
              and os.path.isdir(wt_path), wt_path or "no path in the start result")
    if wt_path and os.path.isdir(wt_path):
        branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt_path).stdout.strip()
        rep.check("worktree branch is agent/umni/e2e-double", branch == "agent/umni/e2e-double", branch)
        origin_main = git("rev-parse", "origin/main", cwd=clone).stdout.strip()
        base = git("merge-base", "HEAD", origin_main, cwd=wt_path).stdout.strip() if origin_main else ""
        rep.check("worktree is based on origin/main", bool(origin_main) and base == origin_main,
                  f"origin/main={origin_main[:12]} merge-base={base[:12]}")
        rep.check("new test file exists IN THE WORKTREE", os.path.isfile(os.path.join(wt_path, "tests", "test_double.py")))
        core = open(os.path.join(wt_path, "umni", "core.py"), encoding="utf-8").read()
        rep.check("worktree umni/core.py defines double()", "def double(x)" in core)
    rep.check("the clone is untouched (no tests/test_double.py, no double())",
              not os.path.exists(os.path.join(clone, "tests", "test_double.py"))
              and "def double" not in open(os.path.join(clone, "umni", "core.py"), encoding="utf-8").read())
    dirty = git("status", "--porcelain", cwd=clone).stdout.strip()
    rep.check("the clone's working tree is clean", not dirty, dirty[:300])

    # ── sandbox evidence in the app log ──────────────────────────────────
    try:
        log_text = open(app_log, encoding="utf-8", errors="replace").read()
    except OSError:
        log_text = ""
    rep.check("app log: [shell-sandbox] available", "[shell-sandbox] available" in log_text,
              next((ln for ln in log_text.splitlines() if "[shell-sandbox]" in ln), "no [shell-sandbox] line")[:300])
    blocked = [ln for ln in log_text.splitlines() if "BLOCKED" in ln and "bash" in ln]
    rep.check("app log: bash was not BLOCKED", not blocked, " | ".join(blocked)[:600])
    bash_out = str((outcomes.get("pytest") or {}).get("result") or "")
    # The bash step ends with a probe: the worktree lives under the data dir,
    # but the app database next to it must not be visible from the sandbox.
    rep.check("bash ran inside the sandbox (the app database is not visible from it)",
              "E2E-SANDBOX-OK" in bash_out and "E2E-SANDBOX-LEAK" not in bash_out,
              bash_out[-300:].replace("\n", " | "))

    # ── the hand-back and the parent's reply ─────────────────────────────
    parent_final = None
    handback = None
    while time.monotonic() < deadline:
        msgs = history(client, sid)
        handback = next((m for m in msgs if m.get("role") == "user"
                         and (m.get("metadata") or {}).get("source") == "worker"), None)
        parent_final = next((m for m in msgs if m.get("role") == "assistant"
                             and (m.get("metadata") or {}).get("source") == "worker_followup"), None)
        if parent_final:
            break
        time.sleep(2)
    rep.check("worker result handed back into the admin chat", bool(handback),
              str((handback or {}).get("content", ""))[:300].replace("\n", " | "))
    content = str((parent_final or {}).get("content") or "")
    rep.check("admin chat wrote a follow-up reply", bool(parent_final), content[:300].replace("\n", " | "))
    # "pytest (worktree): 2 passed in 0.01s"; the label may hold parentheses.
    summary = re.search(r"pytest \(.*\): ([^\n]+)", content)
    rep.check("admin follow-up quotes the worker's result (E2E-WORKER-RESULT + pytest summary)",
              "E2E-PARENT-FINAL" in content and "E2E-WORKER-RESULT" in content and bool(summary)
              and "passed" in (summary.group(1) if summary else ""),
              (summary.group(1).strip() if summary else "no pytest line in the reply"))

    unknown = [r for r in read_jsonl(mock_log) if r.get("kind") in ("unknown", "unhandled_route")
               or str(r.get("step", "")).endswith("_error")]
    rep.check("mock saw no off-script request", not unknown, json.dumps(unknown)[:800])
    return finish()


if __name__ == "__main__":
    sys.exit(main())
