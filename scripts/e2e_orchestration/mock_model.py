"""Scripted OpenAI-compatible model for the orchestration end-to-end test.

It plays two agents from one endpoint and decides every reply from the
request alone (no hidden state), so a restarted app or a retried request gets
the same answer:

* the admin chat: on the user's message it calls ``manage_agent_loadout``
  start for "Lead Engineer"; when the worker's hand-back arrives it answers
  with a final text that quotes the worker's result;
* the worker: ``manage_git`` fetch -> ``manage_agent_worktree`` start ->
  ``edit_file`` + ``write_file`` in the worktree -> ``bash`` pytest ->
  ``manage_agent_worktree`` diff -> a final answer with the pytest summary.

Which step comes next is read from the tool calls and tool results already in
the request's messages. Every request is appended to a JSON-lines log with the
conversation it belongs to, the step chosen and, for the step just finished,
what its tool result said. A request that fits no script gets an
``E2E-MOCK-ERROR`` text reply, which ends that agent's turn and shows up in the
chat, so the driver can say where the flow diverged.

Requests without tools (chat titles, memory extraction, ...) are auxiliary
and get a short neutral reply.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

MODEL_ID = os.environ.get("MOCK_MODEL_ID", "e2e-mock")
# Not a leading "[tag]": agent_loop._spoken_user_text drops lines that start
# like a pasted log line, and with it the loadout name the gate looks for.
ADMIN_MARKER = "(ref E2E-ADMIN)"
TASK_MARKER = "add a function double(x)"
CLONE = os.environ.get("E2E_CLONE", "/home/user/e2e/development/umni")
# The app's data dir. The bash step also checks that its database is NOT
# visible from inside the sandbox, although the worktree under it is.
DATA_DIR = os.environ.get("E2E_DATA_DIR", "/home/user/e2e/data")
WORKTREE_NAME = os.environ.get("E2E_WORKTREE_NAME", "e2e-double")
LOADOUT = os.environ.get("E2E_WORKER_LOADOUT", "Lead Engineer")
# main: pytest runs in the worktree (the real flow). workaround: pytest runs in
# the workspace clone, for when the sandbox cannot see the worktree.
VARIANT = os.environ.get("E2E_VARIANT", "main")
LOG_PATH = os.environ.get("MOCK_LOG", "/tmp/e2e_mock_requests.jsonl")
DUMP_DIR = os.environ.get("MOCK_DUMP_DIR", "")
# Streamed text is split into chunks with this pause between them, so the
# app's stream parser sees more than one delta.
CHUNK_DELAY_S = float(os.environ.get("MOCK_CHUNK_DELAY_S", "0.01"))

WORKER_TASK = (
    f"In {CLONE} add a function double(x) with a test, in an isolated worktree from origin/main; "
    "run the tests; report."
)

app = FastAPI()
_log_lock = threading.Lock()


def log(entry: Dict[str, Any]) -> None:
    entry = {"ts": round(time.time(), 3), **entry}
    with _log_lock:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ── reading the request ─────────────────────────────────────────────────────


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or ""))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return str(content)


def _tool_names(body: Dict[str, Any]) -> List[str]:
    names = []
    for tool in body.get("tools") or []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            names.append(str(fn["name"]))
    return names


def _calls(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every tool call in the history, with its arguments and its result."""
    results: Dict[str, str] = {}
    for msg in messages:
        if msg.get("role") == "tool":
            results[str(msg.get("tool_call_id") or "")] = _text(msg.get("content"))
    calls = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except (TypeError, ValueError):
                args = {"_raw": raw}
            call_id = str(tc.get("id") or "")
            calls.append({"id": call_id, "name": fn.get("name"), "args": args,
                          "result": results.get(call_id)})
    return calls


def _is_handback(text: str) -> bool:
    """The message agent_control._hand_off saves into the parent chat:
    "[Worker <name> finished]\\nTask: ...\\n\\nResult:\\n..." (the agent loop
    may put something in front of it, so not only at the start)."""
    return bool(re.search(r"\[Worker [^\]]*\]\s*\nTask:", text or ""))


def _classify(body: Dict[str, Any]) -> str:
    messages = body.get("messages") or []
    users = [_text(m.get("content")) for m in messages if m.get("role") == "user"]
    if not body.get("tools"):
        return "aux"
    if any(_is_handback(u) for u in users) and any(ADMIN_MARKER in u for u in users):
        return "admin_followup"
    if any(ADMIN_MARKER in u for u in users):
        return "admin"
    if users and TASK_MARKER in users[0]:
        return "worker"
    if any(TASK_MARKER in u for u in users):
        return "worker"
    return "unknown"


def _ok(result: Optional[str]) -> bool:
    """Whether a tool result reads as a success."""
    if result is None:
        return False
    text = result.strip()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        if data.get("error"):
            return False
        code = data.get("exit_code")
        return code in (0, None)
    lowered = text.lower()
    if re.search(r'"exit_code"\s*:\s*[1-9]', text) or re.search(r"exit[_ ]code[:= ]+[1-9]", lowered):
        return False
    # The agent loop renders tool results as markdown: "### <tool>: BLOCKED",
    # "**Error:** ...", "**exit_code:** 1".
    first = lowered.splitlines()[0] if lowered else ""
    if ": blocked" in first or ": failed" in first or ": error" in first:
        return False
    if lowered.startswith("error") or '"error":' in lowered or "**error:**" in lowered:
        return False
    if re.search(r"\*\*exit_code:?\*\*:?\s*[1-9]", lowered):
        return False
    return True


_WT_PATH_RE = re.compile(r'"path"\s*:\s*"([^"]+)"')


def _worktree_path(result: Optional[str]) -> Optional[str]:
    """The worktree directory from manage_agent_worktree start's result."""
    if not result:
        return None
    try:
        data = json.loads(result)
    except ValueError:
        data = None
    if isinstance(data, dict):
        wt = data.get("worktree") if isinstance(data.get("worktree"), dict) else data
        for key in ("path", "worktree_path", "worktree"):
            value = wt.get(key) if isinstance(wt, dict) else None
            if isinstance(value, str) and value.startswith("/"):
                return value
    for match in _WT_PATH_RE.finditer(result):
        if WORKTREE_NAME in match.group(1):
            return match.group(1)
    match = re.search(r"(/[^\s\"']*" + re.escape(WORKTREE_NAME) + r")", result)
    return match.group(1) if match else None


_PYTEST_SUMMARY_RE = re.compile(r"(=*\s*)?(\d+ (?:passed|failed|error)[^\n]*)")


def _pytest_summary(result: Optional[str]) -> str:
    if not result:
        return ""
    lines = [ln.strip().strip("=").strip() for ln in result.replace("\\n", "\n").splitlines()]
    for line in reversed(lines):
        if re.search(r"\d+ (passed|failed|errors?)\b", line) or "no tests ran" in line:
            return line[:200]
    return ""


# ── the scripts ─────────────────────────────────────────────────────────────


def _tool(name: str, args: Dict[str, Any], step: str) -> Dict[str, Any]:
    return {"kind": "tool", "name": name, "args": args, "step": step,
            "id": f"call_e2e_{step}_{uuid.uuid4().hex[:6]}"}


def _say(text: str, step: str) -> Dict[str, Any]:
    return {"kind": "text", "text": text, "step": step}


def admin_turn(calls: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    starts = [c for c in calls if c["name"] == "manage_agent_loadout"]
    if not starts:
        return _tool("manage_agent_loadout", {
            "action": "start", "name": LOADOUT, "task": WORKER_TASK, "workspace": CLONE,
        }, "admin_start"), {}
    last = starts[-1]
    if last["result"] is None:
        return _say("E2E-MOCK-ERROR admin: the manage_agent_loadout call has no tool result in the request",
                    "admin_error"), {"prev": "admin_start", "ok": False}
    if not _ok(last["result"]):
        return _say("E2E-MOCK-ERROR admin: manage_agent_loadout start failed: " + last["result"][:1500],
                    "admin_error"), {"prev": "admin_start", "ok": False, "result": last["result"][:4000]}
    run = re.search(r'"run_id"\s*:\s*"([^"]+)"', last["result"])
    return _say(f"E2E-ADMIN-ACK: started {LOADOUT} (run {run.group(1) if run else '?'}); "
                "its result comes back here when it finishes.", "admin_ack"), {
        "prev": "admin_start", "ok": True, "result": last["result"][:4000]}


def admin_followup(messages: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    handback = ""
    for msg in messages:
        text = _text(msg.get("content"))
        if msg.get("role") == "user" and _is_handback(text):
            handback = text
    match = re.search(r"Result:\n(.*?)(?:\n\nThe worker |\n\nReport this result|\Z)", handback, re.S)
    result = (match.group(1) if match else handback).strip()
    if "E2E-WORKER-RESULT" not in result:
        return _say("E2E-MOCK-ERROR admin_followup: the hand-back does not carry the worker's result: "
                    + handback[:1500], "admin_followup_error"), {"ok": False, "handback": handback[:4000]}
    return _say("E2E-PARENT-FINAL: the Lead Engineer finished. Its report:\n> "
                + result.replace("\n", "\n> "), "admin_final"), {"ok": True, "handback": handback[:4000]}


# The worker's plan: (step, tool) in order. Arguments that depend on earlier
# results are built in worker_turn.
WORKER_STEPS = ("fetch", "worktree_start", "edit_core", "write_test", "pytest", "diff")


def _worker_step(call: Dict[str, Any]) -> Optional[str]:
    """Which scripted step a tool call in the history was. Read from the call
    itself rather than from its id, so it holds even if the app re-ids calls."""
    name, args = call.get("name"), call.get("args") or {}
    action = str(args.get("action") or "").lower()
    if name == "manage_git" and action == "fetch":
        return "fetch"
    if name == "manage_agent_worktree" and action == "start":
        return "worktree_start"
    if name == "manage_agent_worktree" and action == "diff":
        return "diff"
    return {"edit_file": "edit_core", "write_file": "write_test", "bash": "pytest"}.get(name)


def worker_turn(calls: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    done: Dict[str, Dict[str, Any]] = {}
    for call in calls:
        step = _worker_step(call)
        if step:
            done[step] = call
    if len(calls) > 3 * len(WORKER_STEPS):
        return _say(f"E2E-MOCK-ERROR worker: {len(calls)} tool calls in history, the script has "
                    f"{len(WORKER_STEPS)}; the flow is looping", "worker_error"), {}
    outcome: Dict[str, Any] = {}
    finished = [s for s in WORKER_STEPS if s in done]
    if finished:
        prev = finished[-1]
        res = done[prev]["result"]
        outcome = {"prev": prev, "ok": _ok(res), "result": (res or "")[:6000]}
        if res is None:
            return _say(f"E2E-MOCK-ERROR worker: step {prev} has no tool result in the request "
                        "(tool call ids not echoed back?)", "worker_error"), outcome
    wt = _worktree_path((done.get("worktree_start") or {}).get("result"))
    nxt = next((s for s in WORKER_STEPS if s not in done), None)
    if nxt == "fetch":
        return _tool("manage_git", {"action": "fetch", "repository": CLONE}, "fetch"), outcome
    if nxt == "worktree_start":
        return _tool("manage_agent_worktree", {"action": "start", "repository": CLONE,
                                               "name": WORKTREE_NAME, "base": "origin/main"},
                     "worktree_start"), outcome
    if not wt:
        return _say("E2E-MOCK-ERROR worker: manage_agent_worktree start gave no worktree path: "
                    + str((done.get("worktree_start") or {}).get("result"))[:1500], "worker_error"), outcome
    outcome["worktree"] = wt
    if nxt == "edit_core":
        return _tool("edit_file", {
            "path": f"{wt}/umni/core.py",
            "old_string": "def add(a, b):\n    return a + b\n",
            "new_string": "def add(a, b):\n    return a + b\n\n\ndef double(x):\n    return 2 * x\n",
        }, "edit_core"), outcome
    if nxt == "write_test":
        return _tool("write_file", {
            "path": f"{wt}/tests/test_double.py",
            "content": "from umni.core import double\n\n\ndef test_double():\n    assert double(21) == 42\n",
        }, "write_test"), outcome
    if nxt == "pytest":
        where = wt if VARIANT == "main" else CLONE
        probe = (f"{{ test -e {DATA_DIR}/app.db && echo E2E-SANDBOX-LEAK: the app database is visible "
                 "|| echo E2E-SANDBOX-OK: the app database is not visible; }")
        return _tool("bash", {"command": f"cd {where} && python -m pytest -q && {probe}"}, "pytest"), outcome
    if nxt == "diff":
        return _tool("manage_agent_worktree", {"action": "diff", "repository": CLONE,
                                               "name": WORKTREE_NAME}, "diff"), outcome
    # Everything ran: report.
    summary = _pytest_summary((done.get("pytest") or {}).get("result"))
    failed = [s for s in WORKER_STEPS if not _ok(done[s]["result"])]
    diff_res = (done.get("diff") or {}).get("result") or ""
    files = sorted(set(re.findall(r"(umni/core\.py|tests/test_double\.py)", diff_res)))
    text = (
        "E2E-WORKER-RESULT\n"
        f"worktree: {wt}\n"
        f"pytest ({'worktree' if VARIANT == 'main' else 'workspace clone (workaround variant)'}): "
        f"{summary or 'no pytest summary line in the bash output'}\n"
        f"diff files: {', '.join(files) or 'none reported'}\n"
        f"steps with an error result: {', '.join(failed) or 'none'}"
    )
    return _say(text, "worker_final"), outcome


# ── OpenAI wire format ──────────────────────────────────────────────────────


def _chunk(cid: str, delta: Dict[str, Any], finish: Optional[str] = None) -> str:
    payload = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
               "model": MODEL_ID, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(payload)}\n\n"


def _stream(reply: Dict[str, Any]):
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    yield _chunk(cid, {"role": "assistant", "content": ""})
    if reply["kind"] == "tool":
        args = json.dumps(reply["args"])
        yield _chunk(cid, {"tool_calls": [{"index": 0, "id": reply["id"], "type": "function",
                                           "function": {"name": reply["name"], "arguments": ""}}]})
        for i in range(0, len(args), 80):
            time.sleep(CHUNK_DELAY_S)
            yield _chunk(cid, {"tool_calls": [{"index": 0, "function": {"arguments": args[i:i + 80]}}]})
        yield _chunk(cid, {}, "tool_calls")
    else:
        text = reply["text"]
        for i in range(0, len(text), 40):
            time.sleep(CHUNK_DELAY_S)
            yield _chunk(cid, {"content": text[i:i + 40]})
        yield _chunk(cid, {}, "stop")
    usage = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": MODEL_ID,
             "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}
    yield f"data: {json.dumps(usage)}\n\n"
    yield "data: [DONE]\n\n"


def _completion(reply: Dict[str, Any]) -> Dict[str, Any]:
    message: Dict[str, Any] = {"role": "assistant"}
    if reply["kind"] == "tool":
        message["content"] = None
        message["tool_calls"] = [{"id": reply["id"], "type": "function",
                                  "function": {"name": reply["name"], "arguments": json.dumps(reply["args"])}}]
        finish = "tool_calls"
    else:
        message["content"] = reply["text"]
        finish = "stop"
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion", "created": int(time.time()),
            "model": MODEL_ID, "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}


@app.get("/v1/models")
@app.get("/models")
async def models():
    return {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "e2e",
                                        "context_length": 131072, "max_model_len": 131072}]}


def _stream_scenario(reply: Dict[str, Any], slow: bool):
    """Like _stream, for scenario replies: several tool calls in one round, or
    a text streamed slowly enough to still be running when the app stops."""
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    yield _chunk(cid, {"role": "assistant", "content": ""})
    if "calls" in reply:
        for i, call in enumerate(reply["calls"]):
            yield _chunk(cid, {"tool_calls": [{"index": i, "id": f"call_s_{uuid.uuid4().hex[:8]}", "type": "function",
                                               "function": {"name": call["name"], "arguments": ""}}]})
            yield _chunk(cid, {"tool_calls": [{"index": i, "function": {"arguments": json.dumps(call["args"])}}]})
        yield _chunk(cid, {}, "tool_calls")
    else:
        text = reply["text"]
        step = 8 if slow else 40
        for i in range(0, len(text), step):
            time.sleep(0.5 if slow else CHUNK_DELAY_S)
            yield _chunk(cid, {"content": text[i:i + step]})
        yield _chunk(cid, {}, "stop")
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages") or []
    if body.get("tools"):
        import asyncio

        try:
            from scenario_scripts import env_context, scenario_turn

            scen = scenario_turn(body, env_context())
        except Exception as exc:  # a broken scenario must not take the main flow down
            log({"kind": "scenario_error", "error": repr(exc)[:2000]})
            scen = None
        if scen is not None:
            if scen["delay"]:
                await asyncio.sleep(scen["delay"])
            log({"kind": "scenario", "scenario": scen["scenario"], "agent": scen["agent"], "tag": scen["tag"],
                 "round": scen["round"], "ts": time.time(), "tools": _tool_names(body),
                 "results": [{"name": r["name"], "args": r["args"], "result": (r["result"] or "")[:4000]}
                             for r in scen["results"]],
                 "reply": scen["reply"]})
            if body.get("stream"):
                return StreamingResponse(_stream_scenario(scen["reply"], scen["slow"]),
                                         media_type="text/event-stream")
            if "calls" in scen["reply"]:
                return JSONResponse({"id": "s", "object": "chat.completion", "model": MODEL_ID, "choices": [{
                    "index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                    "tool_calls": [{"id": f"call_s_{i}", "type": "function", "function": {
                        "name": c["name"], "arguments": json.dumps(c["args"])}}
                        for i, c in enumerate(scen["reply"]["calls"])]}}]})
            return JSONResponse(_completion(_say(scen["reply"]["text"], "scenario")))
    kind = _classify(body)
    if DUMP_DIR:
        # Full request bodies, one file per request, for reading what each
        # agent was actually sent.
        os.makedirs(DUMP_DIR, exist_ok=True)
        name = f"{time.time():.3f}-{kind}.json"
        with open(os.path.join(DUMP_DIR, name), "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=1, ensure_ascii=False)
    tools = _tool_names(body)
    outcome: Dict[str, Any] = {}
    if kind == "admin":
        reply, outcome = admin_turn(_calls(messages))
    elif kind == "admin_followup":
        reply, outcome = admin_followup(messages)
    elif kind == "worker":
        reply, outcome = worker_turn(_calls(messages))
    elif kind == "aux":
        system = next((_text(m.get("content")) for m in messages if m.get("role") == "system"), "")
        wants_json = "json" in (system + _text((messages or [{}])[-1].get("content"))).lower()
        reply = _say("[]" if wants_json else "E2E orchestration test", "aux")
    else:
        users = [_text(m.get("content"))[:300] for m in messages if m.get("role") == "user"]
        reply = _say("E2E-MOCK-ERROR: request fits neither the admin nor the worker script "
                     f"(user messages: {users[:3]})", "unknown")
    log({
        "kind": kind, "step": reply["step"], "stream": bool(body.get("stream")),
        "n_messages": len(messages), "n_tools": len(tools),
        "tools": tools if kind in ("admin", "worker", "admin_followup", "unknown") else tools[:5],
        "reply": ({"tool": reply["name"], "args": reply["args"], "id": reply["id"]}
                  if reply["kind"] == "tool" else {"text": reply["text"][:2000]}),
        **({"outcome": outcome} if outcome else {}),
        **({"system_head": next((_text(m.get("content"))[:300] for m in messages
                                 if m.get("role") == "system"), "")} if kind in ("aux", "unknown") else {}),
        "roles": "".join((m.get("role") or "?")[0] for m in messages),
        "user_heads": [_text(m.get("content"))[:160] for m in messages if m.get("role") == "user"][-4:],
    })
    if body.get("stream"):
        return StreamingResponse(_stream(reply), media_type="text/event-stream")
    return JSONResponse(_completion(reply))


@app.get("/slots")
async def slots():
    # The app probes llama.cpp's /slots on local endpoints; a 404 means "not llama.cpp".
    return JSONResponse({"error": {"message": "not llama.cpp"}}, status_code=404)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def other(path: str, request: Request):
    log({"kind": "unhandled_route", "method": request.method, "path": "/" + path})
    return JSONResponse({"error": {"message": f"mock has no route /{path}"}}, status_code=404)


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7811)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
