"""Scripted agents for the e2e scenario suite (scenarios.py drives it).

mock_model.py hands a request here when its conversation carries a scenario
marker; everything else keeps the main flow's script. A scenario is:

* the chat's script, per user turn: a list of rounds, each a batch of tool
  calls or a final text. The round is picked by how many tool-call rounds the
  conversation already has since its last user message, so a restarted app or
  a retried request gets the same answer;
* worker scripts, keyed by the tag in the worker's task "(scenario NAME TAG)";
* the chat's follow-up after a worker hands back (or after the "already
  covered?" note), which names every worker result it was given.

Arguments are templates: {clone}, {data}, {root}, {wt} (the worktree the last
``manage_agent_worktree start`` made), {request_id} (the last request_publish),
{code} and {rid} (from the chat's latest user message).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

SCENARIO_RE = re.compile(r"\(ref E2E-S:([a-z_]+)\)")
WORKER_RE = re.compile(r"\(scenario ([a-z_]+)(?: ([A-Za-z0-9_-]+))?\)")
# The quoted request runs to the end of a worker's task, or, in a parent's
# copy of the task inside a hand-back, up to the "\n\nResult:" that follows.
PERSON_REQUEST_RE = re.compile(r"\n\nThe person's request, in their own words.*?(?=\n\nResult:|\Z)", re.S)

LEAD = "Lead Engineer"


def _start(task: str, **extra: Any) -> Dict[str, Any]:
    return {"name": "manage_agent_loadout", "args": {"action": "start", "name": LEAD, "task": task, **extra}}


def _bash(command: str) -> Dict[str, Any]:
    return {"name": "bash", "args": {"command": command}}


def _call(tool: str, **args: Any) -> Dict[str, Any]:
    return {"name": tool, "args": args}


def _calls(*calls: Dict[str, Any], delay: float = 0.0) -> Dict[str, Any]:
    return {"calls": list(calls), "delay": delay}


def _text(text: str, delay: float = 0.0, slow: bool = False) -> Dict[str, Any]:
    return {"text": text, "delay": delay, "slow": slow}


_ACK = _text("S-ACK: the worker is running; its result comes back here.")
_WT = "manage_agent_worktree"

SCENARIOS: Dict[str, Dict[str, Any]] = {
    # The chat is bound to a folder that holds the app's data (the 2026-09-29
    # chat was bound to /app). The worker, which has no private-vault grant,
    # must still get bash: preflight binds it to the repository the task names.
    "broad_parent": {
        "chat": [[_calls(_start("In umni, print the working directory and what the shell can see. "
                                "(scenario broad_parent)")), _ACK]],
        "workers": {"": [_calls(_bash("pwd; test -e {data}/app.db && echo S-LEAK || echo S-NO-DB")),
                         _text("S-DONE broad_parent")]},
    },
    # origin is scp-style (git@host:owner/repo). manage_git fetches it over
    # HTTPS; the same fetch from bash has no key and must point at manage_git.
    "ssh_remote": {
        "chat": [[_calls(_start("In umni, fetch origin. (scenario ssh_remote)", workspace="{clone}")), _ACK]],
        "workers": {"": [_calls(_call("manage_git", action="fetch", repository="{clone}")),
                         _calls(_bash("cd {clone} && git fetch origin")),
                         _text("S-DONE ssh_remote")]},
    },
    # The user's work is on a local branch with commits GitHub does not have.
    "feature_base": {
        "chat": [[_calls(_start("In umni, continue from feature/next. (scenario feature_base)",
                                workspace="{clone}")), _ACK]],
        "workers": {"": [_calls(_call(_WT, action="start", repository="{clone}", name="s-feature",
                                      base="feature/next")),
                         _calls(_bash("test -f {wt}/umni/feature.py && echo S-HAS-FEATURE || echo S-NO-FEATURE")),
                         _text("S-DONE feature_base")]},
    },
    # Commands that wait on a terminal: a pager, an editor, a stdin read.
    "interactive": {
        "chat": [[_calls(_start("In umni, run a few git commands. (scenario interactive)",
                                workspace="{clone}")), _ACK]],
        "workers": {"": [_calls(_call(_WT, action="start", repository="{clone}", name="s-inter",
                                      base="origin/main")),
                         _calls(_bash("cd {wt} && git log && echo S-LOG-DONE")),
                         _calls(_bash("cd {wt} && echo x > s.txt && git add s.txt && git commit; "
                                      "echo S-COMMIT-RC=$?")),
                         _calls(_bash("read line; echo S-READ-DONE:$line")),
                         _text("S-DONE interactive")]},
    },
    # A worker stuck in a long command, stopped from its own chat.
    "stop_worker": {
        "chat": [[_calls(_start("In umni, wait a long time. (scenario stop_worker)", workspace="{clone}")), _ACK]],
        "workers": {"": [_calls(_bash("sleep 600; echo S-LATE")), _text("S-DONE stop_worker")]},
    },
    # The same, stopped from the chat that started it once that chat is idle.
    "stop_parent": {
        "chat": [[_calls(_start("In umni, wait a long time. (scenario stop_parent)", workspace="{clone}")), _ACK]],
        "workers": {"": [_calls(_bash("sleep 600; echo S-LATE")), _text("S-DONE stop_parent")]},
    },
    # Two workers at once: one summarising reply once both are back.
    "parallel": {
        "chat": [[_calls(_start("In umni, report A. (scenario parallel A)", workspace="{clone}"),
                         _start("In umni, report B. (scenario parallel B)", workspace="{clone}")), _ACK]],
        "workers": {"A": [_text("S-RESULT-A", delay=1)], "B": [_text("S-RESULT-B", delay=4)]},
    },
    # Commit in a worktree of a third-party repository and ask to publish. The
    # push itself goes to github.com, which the suite does not reach; what is
    # checked is that the commit lands and request_publish either proceeds or
    # names what is missing.
    "publish": {
        "chat": [
            [_calls(_call(_WT, action="start", repository="{clone}", name="s-publish", base="origin/main")),
             _calls(_call("write_file", path="{wt}/PUBLISHED.md", content="published by the e2e suite\n")),
             _calls(_call(_WT, action="commit", repository="{clone}", name="s-publish", message="e2e publish")),
             _calls(_call(_WT, action="request_publish", repository="{clone}", name="s-publish",
                          title="E2E publish", body="From the e2e scenario suite.")),
             _text("S-REQUESTED {request_id}")],
        ],
        "workers": {},
    },
    # The shell's own setting, separate from vault access (src/shell_access.py).
    # A chat with the vault grant and the default Sandboxed shell: bash still
    # cannot see the app's data.
    "vault_sandboxed": {
        "chat": [[_calls(_bash("pwd; test -e {data}/app.db && echo S-SEES-DATA || echo S-NO-DATA")),
                  _text("S-DONE vault_sandboxed")]],
        "workers": {},
    },
    # A chat without the vault grant, set to Full server shell: bash runs on the host.
    "host_shell": {
        "chat": [[_calls(_bash("test -e {data}/app.db && echo S-SEES-DATA || echo S-NO-DATA")),
                  _text("S-DONE host_shell")]],
        "workers": {},
    },
    # A worker bound to a folder the sandbox refuses, with no repository named:
    # it still gets bash, in a scratch folder.
    "scratch_shell": {
        "chat": [[_calls(_start("Say where your shell runs. (scenario scratch_shell)")), _ACK]],
        "workers": {"": [_calls(_bash("pwd; test -e {data}/app.db && echo S-SEES-DATA || echo S-NO-DATA")),
                         _text("S-DONE scratch_shell")]},
    },
    # The 2026-09-29 dead end: a worker stops on something the chat that
    # started it can settle, and says so. The chat's follow-up sends the SAME
    # worker back (send_to_session, mode agent: its own history and workspace)
    # instead of reporting "blocked" and waiting for the user to type "retry".
    "continue_blocked": {
        "chat": [[_calls(_start("In umni, write the marker file. (scenario continue_blocked)",
                                workspace="{clone}")), _ACK]],
        "followup": [_calls(_call("send_to_session", session_id="{worker}", mode="agent",
                                  message="Go ahead and write it. (scenario continue_blocked resumed)")),
                     _text("S-FOLLOWUP-RESUMED continue_blocked")],
        "workers": {"": [_calls(_bash("echo S-FIRST-RUN")),
                         _text("Blocked before writing the marker.\nNeeds parent: the go-ahead to write it")],
                    "resumed": [_calls(_bash("echo S-RESUMED-RUN")), _text("S-DONE continue_blocked")]},
    },
    # A long reply still streaming when the app is stopped (redeploy).
    "slow": {
        "chat": [[_text("S-SLOW " + " ".join(f"part{i}" for i in range(120)), slow=True)]],
        "workers": {},
    },
}


def _fill(value: Any, ctx: Dict[str, str]) -> Any:
    if isinstance(value, str):
        return re.sub(r"\{(\w+)\}", lambda m: str(ctx.get(m.group(1), m.group(0))), value)
    if isinstance(value, dict):
        return {k: _fill(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, ctx) for v in value]
    return value


def _text_of(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or p.get("content") or "") if isinstance(p, dict) else str(p)
                         for p in content)
    return "" if content is None else str(content)


def _results(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every tool call in the conversation with its result, in order."""
    out: Dict[str, str] = {m.get("tool_call_id") or "": _text_of(m.get("content"))
                           for m in messages if m.get("role") == "tool"}
    calls = []
    for m in messages:
        for tc in (m.get("tool_calls") or []) if m.get("role") == "assistant" else []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            calls.append({"name": fn.get("name"), "args": args, "result": out.get(tc.get("id") or "")})
    return calls


def _rounds_since_last_user(messages: List[Dict[str, Any]]) -> int:
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    return sum(1 for m in messages[last_user + 1:] if m.get("role") == "assistant" and m.get("tool_calls"))


def _context(messages: List[Dict[str, Any]], env: Dict[str, str]) -> Dict[str, str]:
    ctx = dict(env)
    for call in _results(messages):
        res = call.get("result") or ""
        if call["name"] == _WT and (call["args"] or {}).get("action") == "start":
            # The result lists every worktree of the repository; take the one started.
            wanted = re.escape(str((call["args"] or {}).get("name") or ""))
            m = re.search(r'"path"\s*:\s*"([^"]*agent_worktrees[^"]*/' + wanted + r')"', res)
            if m:
                ctx["wt"] = m.group(1)
        if call["name"] == _WT and (call["args"] or {}).get("action") == "request_publish":
            m = re.search(r'"(?:request_id|id)"\s*:\s*"([^"]+)"', res)
            if m:
                ctx["request_id"] = m.group(1)
    users = [_text_of(m.get("content")) for m in messages if m.get("role") == "user"]
    last = users[-1] if users else ""
    for key, pattern in (("code", r"approval code:\s*(\S+)"), ("rid", r"request:\s*(\S+)")):
        m = re.search(pattern, last)
        if m:
            ctx[key] = m.group(1)
    return ctx


def _is_handback(text: str) -> bool:
    return bool(re.search(r"\[Worker [^\]]*\]\s*\nTask:", text or ""))


def scenario_turn(body: Dict[str, Any], env: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """The scripted reply for a scenario request, or None when it is not one.

    Returns {"scenario", "agent", "tag", "round", "reply": {"calls": [...]} |
    {"text": ...}, "delay", "slow", "results"}.
    """
    messages = body.get("messages") or []
    # A worker's task ends with the person's request, quoted verbatim since
    # 2026-10-01 (loadout_tools.person_request_for_worker). That quote carries
    # the CHAT's marker, so route on the brief alone or every worker runs the
    # chat's script.
    users = [PERSON_REQUEST_RE.sub("", _text_of(m.get("content")))
             for m in messages if m.get("role") == "user"]
    chat_marker = next((SCENARIO_RE.search(u) for u in users if SCENARIO_RE.search(u)), None)
    results = _results(messages)
    ctx = _context(messages, env)
    if chat_marker:
        name = chat_marker.group(1)
        spec = SCENARIOS.get(name)
        if spec is None:
            return None
        last = users[-1] if users else ""
        if spec.get("followup") and _is_handback(last):
            # A scripted follow-up; {worker} is the session the hand-back
            # names for sending the same worker back.
            named = re.search(r'session_id "([^"]+)"', last)
            if named:
                ctx["worker"] = named.group(1)
            idx = _rounds_since_last_user(messages)
            step = spec["followup"][min(idx, len(spec["followup"]) - 1)]
            agent, tag = "followup", ""
        elif _is_handback(last) or "[[no-update]]" in last:
            handed = [re.search(r"Result:\n(.*?)(?:\n\n|\Z)", u, re.S) for u in users if _is_handback(u)]
            got = [h.group(1).strip()[:200] for h in handed if h]
            return {"scenario": name, "agent": "followup", "tag": "", "round": 0, "results": results,
                    "reply": {"text": f"S-FOLLOWUP {name}: " + " | ".join(got)}, "delay": 0, "slow": False}
        else:
            turn = sum(1 for u in users if SCENARIO_RE.search(u)) - 1
            rounds = spec["chat"][min(turn, len(spec["chat"]) - 1)]
            idx = _rounds_since_last_user(messages)
            step = rounds[min(idx, len(rounds) - 1)]
            agent, tag = "chat", ""
    else:
        # The latest marker: a worker sent back with a new instruction runs
        # that instruction's script ("(scenario NAME resumed)").
        worker_marker = next((WORKER_RE.search(u) for u in reversed(users) if WORKER_RE.search(u)), None)
        if not worker_marker:
            return None
        name, tag = worker_marker.group(1), worker_marker.group(2) or ""
        spec = SCENARIOS.get(name)
        if spec is None or tag not in spec["workers"]:
            return None
        rounds = spec["workers"][tag]
        idx = sum(1 for m in messages if m.get("role") == "assistant" and m.get("tool_calls"))
        step = rounds[min(idx, len(rounds) - 1)]
        agent = "worker"
    if "calls" in step:
        reply = {"calls": [_fill(c, ctx) for c in step["calls"]]}
    else:
        reply = {"text": _fill(step["text"], ctx)}
    return {"scenario": name, "agent": agent, "tag": tag, "round": idx, "results": results,
            "reply": reply, "delay": step.get("delay", 0), "slow": step.get("slow", False)}


def env_context() -> Dict[str, str]:
    root = os.environ.get("E2E_ROOT", os.path.expanduser("~/e2e"))
    return {"clone": os.environ.get("E2E_CLONE", os.path.join(root, "development", "umni")),
            "data": os.environ.get("E2E_DATA_DIR", os.path.join(root, "data")), "root": root}
