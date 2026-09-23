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


# ── Round-budget wrap-up (an agent profile's explicit max_rounds) ─────────


def _run_budget_loop(monkeypatch, *, wrap_up_round, answer_round=None, synth=""):
    """A native tool-calling model that calls a new tool every round (never
    stuck, so the loop-breaker stays quiet) until `answer_round`, if any,
    where it answers instead."""
    calls = []
    executed = []

    async def _fake_stream(_candidates, messages, **kwargs):
        calls.append({"tools": kwargs.get("tools"), "messages": list(messages)})
        n = len(calls)
        if answer_round and n >= answer_round:
            yield f'data: {json.dumps({"delta": "All done: here is the answer."})}\n\n'
        else:
            call = {"name": "update_plan", "arguments": json.dumps({"plan": f"- [ ] step {n}"})}
            yield f'data: {json.dumps({"type": "tool_calls", "calls": [call]})}\n\n'
        yield "data: [DONE]\n\n"

    async def _fake_exec(block, *a, **k):
        executed.append(len(calls))
        return (block.tool_type, {"output": f"ok {len(calls)}", "exit_code": 0})

    async def _fake_synth(**kwargs):
        return synth

    import src.llm_core as llm_core
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)
    monkeypatch.setattr(llm_core, "llm_call_async", _fake_synth)

    gen = al.stream_agent_loop(
        "https://api.openai.com/v1", "gpt-4o",
        [{"role": "user", "content": "do a long multi-step task"}],
        max_rounds=wrap_up_round,
        relevant_tools={"update_plan"},
        wrap_up_round=wrap_up_round,
    )
    return _types(_collect(gen)), calls, executed


def test_round_budget_wraps_up_with_a_tool_free_answer_round(monkeypatch):
    _patch_common(monkeypatch)
    events, calls, executed = _run_budget_loop(
        monkeypatch, wrap_up_round=3, synth="Found A and B. Unfinished: C was not checked.")

    budget = [e for e in events if e.get("type") == "round_budget_reached"]
    assert budget == [{"type": "round_budget_reached", "round": 3, "budget": 3}], events
    # Rounds 1 and 2 ran their tools; round 3 was the last model call and its
    # tool call (the model kept trying) was discarded, not executed.
    assert len(calls) == 3
    assert executed == [1, 2]
    assert calls[0]["tools"] and calls[1]["tools"] and not calls[2]["tools"]
    wrap_msg = calls[2]["messages"][-1]
    # Delivered as a tail harness directive rather than a mid-turn `role:
    # system` message: llm_core hoists every system message into the
    # instructions block at the front of the request, so appending one here
    # would rewrite the cached prompt prefix on the longest round of the run
    # (specs/prompt-prefix-stability.md). The instruction itself is unchanged.
    assert wrap_msg["role"] == "user"
    assert wrap_msg["content"].startswith("[Harness directive")
    assert "round budget (3 rounds)" in wrap_msg["content"]
    assert "unfinished" in wrap_msg["content"]
    # It ends with an answer, not a cut-off.
    text = "".join(e.get("delta", "") for e in events if isinstance(e.get("delta"), str))
    assert "Unfinished: C was not checked." in text
    assert not any(e.get("type") in ("rounds_exhausted", "round_budget_unanswered", "loop_breaker_triggered")
                   for e in events), events


def test_round_budget_with_no_answer_says_so(monkeypatch):
    _patch_common(monkeypatch)
    events, calls, _executed = _run_budget_loop(monkeypatch, wrap_up_round=2, synth="")

    assert len(calls) == 2
    assert any(e.get("type") == "round_budget_unanswered" and e.get("budget") == 2 for e in events), events
    # The loop's existing fallback line still closes the turn.
    assert any("couldn't pull a clean" in (e.get("delta") or "") for e in events)


def test_no_wrap_up_round_keeps_rounds_advisory(monkeypatch):
    _patch_common(monkeypatch)
    events, calls, executed = _run_budget_loop(monkeypatch, wrap_up_round=0, answer_round=6)

    assert not any(str(e.get("type", "")).startswith("round_budget") for e in events), events
    assert len(calls) == 6 and executed == [1, 2, 3, 4, 5]
    assert all(c["tools"] for c in calls)
    assert not any("round budget" in str(m.get("content")) for m in calls[-1]["messages"])


def test_max_rounds_alone_does_not_wrap_up(monkeypatch):
    """Foreground chat passes max_rounds only; it must stay advisory."""
    _patch_common(monkeypatch)
    calls = []

    async def _fake_stream(_candidates, messages, **kwargs):
        calls.append(1)
        text = ("done." if len(calls) >= 4
                else '```update_plan\n{"plan":"- [ ] step %d"}\n```' % len(calls))
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m", [{"role": "user", "content": "go"}],
        max_rounds=2, relevant_tools={"update_plan"},
    )))
    assert len(calls) == 4
    assert not any(str(e.get("type", "")).startswith("round_budget") for e in events), events


# ── Duplicate-call guard ─────────────────────────────────────────────────
#
# Written on 2026-09-16 for a turn that spent rounds 3-6 on four identical
# `execute_code` calls; `_detect_runaway_call` needs 15 identical calls and the
# stall detector needs four text-free repeats in a row, so a six-round burn was
# invisible to both. The 2026-09-18 upstream-core re-sync (c72cb40c) replaced
# the round loop and took the guard's call sites with it — the three helpers
# stayed in the module with no caller, and these tests went with the call
# sites, so nothing noticed. On 2026-09-23 a turn spent rounds 1-4 on
# `discover_tools` and the guard did not fire, because there was no guard.


def _run_repeat_loop(monkeypatch, round_text, *, tools, max_rounds=4):
    """Drive the real round loop with a model that emits the same call forever."""
    ran = []

    async def _fake_exec(block, *a, **k):
        ran.append((block.tool_type, (block.content or "").strip()))
        return (block.tool_type, {"output": f"run #{len(ran)}", "exit_code": 0})

    _patch_common(monkeypatch)
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)

    async def _fake_stream(_candidates, messages, **kwargs):
        yield f'data: {json.dumps({"delta": round_text})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "find the thing"}],
        max_rounds=max_rounds,
        relevant_tools=set(tools),
    )
    return ran, _types(_collect(gen))


def test_an_identical_repeat_is_answered_from_the_memo(monkeypatch):
    """Same tool, same arguments, nothing mutating in between: run once."""
    call = '```search_documents\n{"query": "tool routing"}\n```'
    ran, events = _run_repeat_loop(monkeypatch, call, tools={"search_documents"})

    assert [name for name, _ in ran] == ["search_documents"], ran
    outputs = [e.get("output") or "" for e in events if e.get("type") == "tool_output"]
    assert len(outputs) > 1, "the model kept calling; only the executions are deduped"
    assert any("you already made this exact call this turn" in text for text in outputs), outputs
    # The first result is what the repeats are answered with.
    assert any("run #1" in text for text in outputs[1:]), outputs


def test_the_guard_never_withholds_a_poll_a_retry_or_a_reread():
    """A false positive here withholds a call the model genuinely needed, so
    the three exemptions are the load-bearing half of this guard."""
    memo = {}
    sig = al._dedupe_signature("read_file", '{"path": "a.md"}')
    al._record_call_result(sig, "read_file", "contents", memo, '{"path": "a.md"}')
    assert al._is_duplicate_call(sig, "read_file", memo, '{"path": "a.md"}') is True

    # 1. Polling, by allowlist, by name shape, and by the `action` argument --
    #    an MCP name carries a per-server hash and can never be enumerated.
    for tool, args in (
        ("manage_bg_jobs", "{}"),
        ("mcp__77d1a280__firecrawl_check_crawl_status", '{"id": "1"}'),
        ("delegate_to_claude_code", '{"action": "poll", "task_id": "1"}'),
    ):
        poll_sig = al._dedupe_signature(tool, args)
        al._record_call_result(poll_sig, tool, "still running", memo, args)
        assert al._is_duplicate_call(poll_sig, tool, memo, args) is False, tool

    # 2. A mutating call drops the memo, so a re-read after an edit is correct.
    write_args = '{"path": "a.md", "content": "x"}'
    al._record_call_result(
        al._dedupe_signature("write_file", write_args), "write_file", "ok", memo, write_args,
    )
    assert al._is_duplicate_call(sig, "read_file", memo, '{"path": "a.md"}') is False

    # 3. A failed call is never memoised at all, so the retry runs.
    fresh = {}
    assert al._is_duplicate_call(sig, "read_file", fresh, '{"path": "a.md"}') is False

    # Key on the FULL arguments: two long calls sharing a prefix are different
    # calls, and suppressing the second is the false positive to avoid.
    long_a = '{"cmd": "' + "x" * 200 + 'a"}'
    long_b = '{"cmd": "' + "x" * 200 + 'b"}'
    assert al._dedupe_signature("bash", long_a) != al._dedupe_signature("bash", long_b)
    # ...and a reordered key is the same call.
    assert (al._dedupe_signature("bash", '{"a": 1, "b": 2}')
            == al._dedupe_signature("bash", '{"b": 2, "a": 1}'))
