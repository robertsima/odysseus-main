"""Regression: what ends a run.

A round count no longer ends anything (this fork, 2026-09-17). Every round
ceiling stopped an agent in the middle of a task it was still working on, so
`stream_agent_loop` iterates until the work is done, and the progress-based
guards decide when that is not happening: the loop-breaker's stall detector,
the runaway-call guard, the per-run tool-call ceiling, and the user's stop
control. `rounds_exhausted` is kept in the loop for a ceiling that might be
reintroduced, and a normal finish must still never emit it.
"""

import asyncio
import json

import src.agent_loop as al


def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _types(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _patch_common(monkeypatch):
    # Skip RAG/tool-index, MCP, and settings lookups; keep the real loop body,
    # _resolve_tool_blocks, and parse_tool_blocks.
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)

    async def _fake_exec(block, *a, **k):
        return (block.tool_type, {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)


def _run_loop(monkeypatch, round_text, max_rounds=2):
    async def _fake_stream(_candidates, messages, **kwargs):
        yield f'data: {json.dumps({"delta": round_text})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "do a long multi-step task"}],
        max_rounds=max_rounds,
        relevant_tools={"bash"},
    )
    return _types(_collect(gen))


def test_a_round_cap_no_longer_cuts_a_run_off(monkeypatch):
    """The agent keeps working past the advisory budget it was given.

    A worker asked for 2 rounds and still calling tools used to stop there with
    the task half done. Now the run continues and is ended by the loop-breaker,
    a limit that can tell working from stuck.
    """
    _patch_common(monkeypatch)
    # A system-owned interaction result keeps this a loop-control test: Bash
    # output is workspace-derived and pauses for exact user approval before a
    # later Bash call. Repeating the same call with no text is what the
    # stall detector counts as stuck.
    events = _run_loop(
        monkeypatch,
        '```update_plan\n{"plan":"- [ ] keep going"}\n```',
        max_rounds=2,
    )
    assert not any(e.get("type") == "rounds_exhausted" for e in events), events
    assert any(e.get("type") == "agent_step" and e.get("round", 0) > 2 for e in events), events
    assert any(e.get("type") == "loop_breaker_triggered" for e in events), events


def test_no_rounds_exhausted_on_normal_finish(monkeypatch):
    _patch_common(monkeypatch)
    # A plain answer (no tool block) -> done-break on round 1 -> no event.
    events = _run_loop(monkeypatch, "All done, here is your answer.", max_rounds=2)
    assert not any(e.get("type") == "rounds_exhausted" for e in events), events


def test_emits_intent_nudge_exhausted_when_cap_is_exhausted(monkeypatch):
    _patch_common(monkeypatch)

    events = _run_loop(monkeypatch, "Let me check the logs", max_rounds=5)

    guard = next((e for e in events if e.get("type") == "intent_nudge_exhausted"), None)
    assert guard is not None, events
    assert guard["reason"] == "intent_without_action_nudge_cap"
    assert guard["nudges"] == 2


def test_emits_loop_breaker_triggered_when_loop_breaker_trips(monkeypatch):
    _patch_common(monkeypatch)

    events = _run_loop(
        monkeypatch,
        '```update_plan\n{"plan":"- [ ] keep going"}\n```',
        max_rounds=6,
    )

    guard = next((e for e in events if e.get("type") == "loop_breaker_triggered"), None)
    assert guard is not None, events
    assert guard["reason"] == "loop_breaker_stall"
