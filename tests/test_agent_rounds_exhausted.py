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


# ── Duplicate-call guard ─────────────────────────────────────────────────────
#
# Repro (2026-09-16): with its delegation tools missing from the schema list,
# a turn latched onto a connected Penpot MCP server and spent rounds 3-6 on
# four identical `execute_code` calls and two `penpot_api_info` calls — one
# call and 37-44 output tokens per round — then gave up in prose on round 7.
# `_detect_runaway_call` only trips at 15 identical calls and the stall
# detector needs four text-free repeats in a row, so a six-round burn was
# invisible. An exact repeat (same tool, same arguments) is now answered from
# the earlier result instead of re-run.


def test_identical_call_is_executed_once_and_answered_from_the_memo(monkeypatch):
    _patch_common(monkeypatch)
    calls = []

    async def _counting_exec(block, *a, **k):
        calls.append(block.content)
        return ("bash", {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _counting_exec, raising=False)

    events = _run_loop(monkeypatch, "```bash\necho hi\n```", max_rounds=3)

    assert len(calls) == 1, f"the repeat was executed again: {calls}"
    dup = [e for e in events
           if e.get("type") == "tool_output"
           and "already made this exact call" in (e.get("output") or "")]
    assert dup, "the repeat must be answered, not silently dropped"
    assert "ok" in dup[0]["output"], "the earlier result has to come back with it"


def test_the_model_is_told_once_that_it_repeated_itself(monkeypatch):
    """Silently short-circuiting teaches the model nothing — it has to be told,
    through the same harness-directive channel the self-unblock uses, and only
    up to the cap."""
    _patch_common(monkeypatch)
    directives = []
    _real = al._harness_directive

    def _spy(text):
        directives.append(str(text))
        return _real(text)
    monkeypatch.setattr(al, "_harness_directive", _spy, raising=False)

    _run_loop(monkeypatch, "```bash\necho hi\n```", max_rounds=8)

    repeats = [d for d in directives if "identical to" in d]
    assert repeats, directives
    assert len(repeats) <= 2, f"the nudge is capped, not repeated forever: {len(repeats)}"


def test_distinct_calls_are_never_suppressed(monkeypatch):
    """The guard must only ever fire on an EXACT repeat: real multi-step work
    (a file hunt, a build->test->fix cycle) is distinct calls all the way down."""
    _patch_common(monkeypatch)
    calls = []

    async def _counting_exec(block, *a, **k):
        calls.append(block.content)
        return ("bash", {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _counting_exec, raising=False)

    seq = iter(["```bash\necho one\n```", "```bash\necho two\n```",
                "```bash\necho three\n```"])

    async def _fake_stream(_candidates, messages, **kwargs):
        yield f'data: {json.dumps({"delta": next(seq, "done")})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    _collect(al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "do a long multi-step task"}],
        max_rounds=3, relevant_tools={"bash"},
    ))

    assert len(calls) == 3, f"a distinct call was suppressed: {calls}"


# ── Which repeats are legitimate ─────────────────────────────────────────────


def test_signature_needs_the_whole_argument_string():
    """`_call_freq` keys on the first 120 characters; the guard cannot. Two
    long bash scripts that differ only at the end are different calls, and
    suppressing the second would be the false positive that matters."""
    long_prefix = "echo " + "x" * 200
    assert (al._dedupe_signature("bash", long_prefix + "a")
            != al._dedupe_signature("bash", long_prefix + "b"))
    assert al._dedupe_signature("bash", "echo hi") == al._dedupe_signature("bash", " echo hi ")
    assert al._dedupe_signature("bash", "echo hi") != al._dedupe_signature("python", "echo hi")


def test_json_arguments_are_canonicalised():
    """Key order is the model's whim, not a different call."""
    assert (al._dedupe_signature("read_file", '{"path": "a.py", "limit": 5}')
            == al._dedupe_signature("read_file", '{"limit": 5, "path": "a.py"}'))


def test_polling_tools_may_repeat_forever():
    """Tailing a serve's output or listing downloads is supposed to return
    something new each time — the identical call IS the workflow."""
    memo = {}
    sig = al._dedupe_signature("tail_serve_output", '{"session_id": "s1"}')
    al._record_call_result(sig, "tail_serve_output", "starting…", memo)
    assert not al._is_duplicate_call(sig, "tail_serve_output", memo)


def test_reading_a_file_again_after_editing_it_is_not_a_duplicate():
    """The named counter-example: read -> edit -> read is correct behaviour,
    so a mutating call drops every observation taken before it."""
    memo = {}
    read = al._dedupe_signature("read_file", '{"path": "a.py"}')
    al._record_call_result(read, "read_file", "v1", memo)
    assert al._is_duplicate_call(read, "read_file", memo)

    al._record_call_result(
        al._dedupe_signature("edit_file", '{"path": "a.py"}'), "edit_file", "ok", memo,
    )

    assert not al._is_duplicate_call(read, "read_file", memo), (
        "the file changed under it; re-reading is the right thing to do"
    )


def test_a_mutating_call_can_still_be_caught_repeating_itself():
    """Clearing the memo must happen BEFORE recording, or a model firing the
    same `bash` twice in a row would never be caught."""
    memo = {}
    sig = al._dedupe_signature("bash", "pytest -q")
    al._record_call_result(sig, "bash", "ok", memo)
    assert al._is_duplicate_call(sig, "bash", memo)

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
