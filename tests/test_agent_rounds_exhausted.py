"""Regression: what ends a run, and what a cut-off run hands back.

A round count no longer ends anything. Every incarnation of a ceiling stopped an
agent in the middle of a task it was still working on, so `stream_agent_loop`
now iterates until the work is done and the progress-based guards below decide
when that is not happening: the stall detector, the runaway/duplicate-call
guard, the per-run tool-call ceiling, and the user's stop control.

`rounds_exhausted` and the headless note that carries it are kept and still
tested, because they are what a caller would need if a ceiling were ever
reintroduced — and because `headless_agent` must handle the frame correctly
whenever it does arrive.
"""

import asyncio
import json

import pytest

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

    This is the whole point: a worker asked for 2 rounds, still calling tools,
    used to stop here with the task half done. Now the run continues and is
    ended by the stall detector instead — a limit that can actually tell the
    difference between working and stuck.
    """
    _patch_common(monkeypatch)
    # Use a system-owned interaction result so this remains a loop-control test:
    # Bash output is workspace-derived and now correctly pauses for exact user
    # approval before a later Bash call.
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


def test_identical_failures_retry_once_then_quote_the_prior_error(monkeypatch):
    """Transient failures deserve one retry, not an unbounded retry loop."""
    _patch_common(monkeypatch)
    calls, directives = [], []

    async def _failing_exec(block, *a, **k):
        calls.append(block.content)
        return ("bash", {"error": "fatal: remote is unavailable", "exit_code": 128})

    real_directive = al._harness_directive
    monkeypatch.setattr(al, "execute_tool_block", _failing_exec, raising=False)
    monkeypatch.setattr(
        al, "_harness_directive",
        lambda text: (directives.append(str(text)) or real_directive(text)),
        raising=False,
    )

    events = _run_loop(monkeypatch, "```bash\ngit push\n```", max_rounds=4)

    assert len(calls) == 2, "the third identical failure must not execute"
    duplicate = [event for event in events if event.get("type") == "tool_output"
                 and "failed twice" in (event.get("output") or "")]
    assert duplicate
    assert "fatal: remote is unavailable" in duplicate[0]["output"]
    failure_directives = [d for d in directives if "third identical attempt" in d]
    assert failure_directives and "fatal: remote is unavailable" in failure_directives[0]


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


# ── The duplicate-call guard and MCP tool names ─────────────────────────────
#
# _DEDUPE_POLLING_TOOLS can only name builtins, but an MCP tool's name carries
# a per-server hash. Real names from the 2026-09-16 logs are used below: a
# firecrawl research job is started and then polled with
# `mcp__77d1a280__firecrawl_agent_status`. Answering the second poll from the
# memo means the job can never be observed finishing.


def _memo_with(name, args="{}", output="first result"):
    memo = {}
    signature = al._dedupe_signature(name, args)
    al._record_call_result(signature, name, output, memo)
    return signature, memo


@pytest.mark.parametrize("name", [
    "mcp__77d1a280__firecrawl_agent_status",
    "mcp__77d1a280__firecrawl_check_crawl_status",
    "mcp__77d1a280__firecrawl_monitor_check",
    "mcp__77d1a280__firecrawl_agent_status",
    "mcp__abc123__job_progress",
    "mcp__abc123__wait_for_run",
])
def test_polling_an_mcp_job_is_never_answered_from_the_memo(name):
    signature, memo = _memo_with(name)
    assert not al._is_duplicate_call(signature, name, memo), name


@pytest.mark.parametrize("name", [
    "mcp__c5ec6d7a__high_level_overview",
    "mcp__93c1b32d__query-docs",
    "mcp__77d1a280__firecrawl_scrape",
])
def test_a_repeated_mcp_read_is_still_caught(name):
    """The guard must keep doing its job — this shape is the original incident,
    where a turn spent four rounds re-reading the same MCP overview."""
    signature, memo = _memo_with(name)
    assert al._is_duplicate_call(signature, name, memo), name


def test_an_mcp_write_invalidates_an_earlier_identical_read():
    """_KNOWN_MUTATING_TOOLS is builtin-only, so without name-shape detection an
    MCP write followed by the same read would serve the pre-write answer."""
    memo = {}
    read = al._dedupe_signature("mcp__c5ec6d7a__high_level_overview", "{}")
    al._record_call_result(read, "mcp__c5ec6d7a__high_level_overview", "before", memo)
    write = al._dedupe_signature("mcp__c5ec6d7a__import_image", '{"f":"x.png"}')
    al._record_call_result(write, "mcp__c5ec6d7a__import_image", "ok", memo)
    assert not al._is_duplicate_call(read, "mcp__c5ec6d7a__high_level_overview", memo)


def test_a_repeated_mcp_write_is_still_caught():
    """Clearing happens before recording, so the write's own signature survives."""
    name = "mcp__c5ec6d7a__execute_code"
    signature, memo = _memo_with(name, '{"code":"1+1"}')
    assert al._is_duplicate_call(signature, name, memo)


def test_the_prefix_stripper_leaves_a_builtin_name_alone():
    assert al._bare_tool_name("read_file") == "read_file"
    assert al._bare_tool_name("mcp__77d1a280__firecrawl_scrape") == "firecrawl_scrape"


# ── A multiplexed tool carries its verb in the arguments ────────────────────
#
# `delegate_to_claude_code {"action": "poll", "task_id": ...}` polls a running
# job under a name with nothing poll-shaped in it, so the name-shape check that
# fixed this for MCP tools could not see it. The second poll of a task was
# answered from the memo with the first one's "still running" — a delegated
# coding job could never be observed finishing. The harness's own audit reports
# exactly this shape: "delegate_to_claude_code was called repeatedly, including
# back-to-back calls and recurring two-minute polls."


@pytest.mark.parametrize("name, args", [
    ("delegate_to_claude_code", '{"action": "poll", "task_id": "t-1"}'),
    ("delegate_to_claude_code", '{"action": "status"}'),
    ("delegate_to_agent", '{"action": "poll", "task_id": "t-1"}'),
    ("manage_agent_worktree", '{"action": "status"}'),
    ("manage_agent_worktree", '{"action": "list_requests"}'),
])
def test_a_poll_action_is_never_answered_from_the_memo(name, args):
    memo = {}
    signature = al._dedupe_signature(name, args)
    al._record_call_result(signature, name, "still running", memo, args)
    assert not al._is_duplicate_call(signature, name, memo, args), f"{name} {args}"


@pytest.mark.parametrize("name, args", [
    ("delegate_to_claude_code", '{"action": "run", "prompt": "fix the bug"}'),
    ("read_file", '{"path": "/a/b.py"}'),
])
def test_real_work_is_still_caught_repeating(name, args):
    memo = {}
    signature = al._dedupe_signature(name, args)
    al._record_call_result(signature, name, "done", memo, args)
    assert al._is_duplicate_call(signature, name, memo, args)


def test_a_mutating_action_invalidates_an_earlier_read():
    """`manage_agent_worktree {"action": "commit"}` changes the world under a
    name that says nothing about it, so the name-shape mutation check missed it
    too and a later identical diff would have served the pre-commit answer."""
    memo = {}
    diff_args = '{"action": "diff"}'
    diff = al._dedupe_signature("manage_agent_worktree", diff_args)
    al._record_call_result(diff, "manage_agent_worktree", "before", memo, diff_args)
    commit_args = '{"action": "commit", "message": "wip"}'
    al._record_call_result(al._dedupe_signature("manage_agent_worktree", commit_args),
                           "manage_agent_worktree", "ok", memo, commit_args)
    assert not al._is_duplicate_call(diff, "manage_agent_worktree", memo, diff_args)


def test_a_non_json_argument_does_not_break_the_check():
    """bash takes a raw command string, not JSON."""
    memo = {}
    signature = al._dedupe_signature("bash", "ls -la")
    al._record_call_result(signature, "bash", "…", memo, "ls -la")
    assert al._dedupe_action("ls -la") == ""
