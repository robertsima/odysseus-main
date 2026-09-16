"""Regression: stream_agent_loop emits `rounds_exhausted` only when the round
cap is hit while still working, and NOT on a normal finish.

The decision is a `for/else` in the loop: the `else` runs only if no `break`
fired (break = done / budget / error). A refactor that adds a stray break or
return, or moves the done-break, could silently flip this. See PR #1999 / #1997.
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
        return ("bash", {"output": "ok", "exit_code": 0})
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


def test_emits_rounds_exhausted_when_cap_hit_mid_task(monkeypatch):
    _patch_common(monkeypatch)
    # Every round returns a tool block -> never "done" -> loop exhausts the cap.
    events = _run_loop(monkeypatch, "```bash\necho hi\n```", max_rounds=2)
    exhausted = next((e for e in events if e.get("type") == "rounds_exhausted"), None)
    assert exhausted is not None, events
    # The frame has to carry how far the turn got, not only that it stopped: a
    # headless caller sees this and the prose and nothing else, and has to tell
    # its parent where to resume from.
    assert exhausted["rounds"] == 2
    assert exhausted["tool_calls"] >= 1, exhausted


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

    events = _run_loop(monkeypatch, "```bash\necho hi\n```", max_rounds=6)

    guard = next((e for e in events if e.get("type") == "loop_breaker_triggered"), None)
    assert guard is not None, events
    assert guard["reason"] == "loop_breaker_stall"


# ── handing a cut-off run back to whoever launched it ──────────────────────
#
# A live chat turns `rounds_exhausted` into a Continue button. A headless run —
# a dashboard worker, a `send_to_session` sub-agent, a background follow-up —
# has nobody to press it: it returns `(prose, tool_events)` and its launcher
# reads that as the finished result. A cut-off round is usually all tool calls
# and no text, so the parent was handed empty prose under a "completed" status
# and never learned there was work left.


def _headless(monkeypatch, frames, **kwargs):
    from src import headless_agent
    import src.model_context as model_context

    async def fake_loop(url, model, messages, **kw):
        for frame in frames:
            yield f"data: {json.dumps(frame)}\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_agent_loop", fake_loop, raising=False)
    # Keep the window probe off the network; it is not what these tests pin.
    monkeypatch.setattr(model_context, "get_context_length", lambda url, model: 400_000)

    class _Sess:
        id, name = "w", "worker"
        endpoint_url, model, headers, owner = "http://llm.test/v1", "m", {}, "alice"

    outcome = {}
    text, events = asyncio.run(headless_agent.run_headless(
        _Sess(), [{"role": "user", "content": "go"}], outcome=outcome, **kwargs))
    return text, events, outcome


def test_headless_hands_back_partial_work_when_rounds_run_out(monkeypatch):
    text, events, outcome = _headless(monkeypatch, [
        {"delta": "Reading the handler"},
        {"type": "tool_output", "tool": "read_file", "output": "...", "exit_code": 0},
        {"type": "tool_output", "tool": "grep", "output": "...", "exit_code": 0},
        {"type": "rounds_exhausted", "rounds": 6, "tool_calls": 2},
    ])

    assert outcome == {"rounds_exhausted": True, "rounds": 6}
    # The work itself still comes back — the point is to keep it, not to warn.
    assert text.startswith("Reading the handler")
    assert len(events) == 2
    # ...and it says where it got to, in the text the parent actually reads.
    assert "ran out of rounds" in text and "6 agent rounds" in text
    assert "2 tool calls completed" in text and "the last was grep" in text


def test_headless_says_nothing_extra_when_the_run_finishes(monkeypatch):
    text, _events, outcome = _headless(monkeypatch, [{"delta": "All done."}])
    assert text == "All done." and outcome == {}
