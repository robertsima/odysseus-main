"""Completed tool exchanges collapse into an execution ledger.

The Sept 15-16 harness audit measured one workflow at 26 rounds / ~96k prompt
tokens with tool results from finished phases still replayed every round. The
ledger collapses those exchanges to the facts that are still true — the path
read, the command run, the id produced, the outcome — plus a `toolout-...` ref
that reopens the verbatim text.

Two properties matter more than the saving and are pinned hardest here:

1. Nothing becomes unrecoverable. Every collapsed result is in the store first,
   and the entry names its ref.
2. Compaction happens in BATCHES, never a little every round. Rewriting a tool
   result edits the middle of an already-cached prompt; doing that per round is
   the failure `specs/prompt-prefix-stability.md` documents.

The vector index is deliberately absent (no ChromaDB), which is the degraded
mode the store must survive.
"""
import tempfile

import pytest

from src.context_compactor import (
    LEDGER_KEEP_ROUNDS,
    LEDGER_MARK,
    LEDGER_MIN_RESULT_CHARS,
    LEDGER_SLACK_ROUNDS,
    compact_tool_exchanges,
    ledger_entry,
)
from src.model_context import estimate_tokens
from src.prompt_security import untrusted_context_message

_REPORT_BACKLOG = pytest.mark.skip(
    reason="Re-port backlog: the execution ledger is not wired into upstream's loop (website/upstream-sync-2026-09-18.md)"
)


@pytest.fixture(autouse=True)
def _store_root(monkeypatch):
    """Root the overflow store in a temp DATA_DIR for every test in this file."""
    import src.constants as constants

    monkeypatch.setattr(constants, "DATA_DIR", tempfile.mkdtemp(), raising=False)


def _read_result(path, size=8000, seed="x"):
    """A `read_file` result shaped the way format_tool_result builds one."""
    body = "\n".join(f"{seed} line {i}" for i in range(size // 12))
    return f"### read_file: {path}\n**content ({len(body)} chars):**\n```\n{body}\n```"


def _bash_result(command, size=8000, exit_code=0):
    body = "\n".join(f"out {i} for {command}" for i in range(size // 20))
    parts = [f"### bash: {command}", f"```\n{body}\n```"]
    if exit_code:
        parts.append(f"**exit_code:** {exit_code}")
    return "\n".join(parts)


def _native_round(idx, result_text, tool="read_file"):
    """One native-function-calling exchange: assistant tool_calls + tool reply."""
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"call_{idx}",
                "type": "function",
                "function": {"name": tool, "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": f"call_{idx}", "content": result_text},
    ]


def _textual_round(result_text):
    return [
        {"role": "assistant", "content": "Let me look."},
        untrusted_context_message("tool execution results", result_text),
    ]


def _transcript(rounds, textual=False):
    msgs = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "Fix the compaction bug."},
    ]
    for i in range(rounds):
        text = _read_result(f"/srv/app/src/module_{i}.py")
        msgs.extend(_textual_round(text) if textual else _native_round(i, text))
    return msgs


# ── The gate: batched, not per-round ────────────────────────────────────────

def test_nothing_is_touched_inside_the_window_plus_slack():
    msgs = _transcript(LEDGER_KEEP_ROUNDS + LEDGER_SLACK_ROUNDS)
    before = [m.get("content") for m in msgs]
    stats = compact_tool_exchanges(msgs)
    assert stats["entries"] == 0
    assert [m.get("content") for m in msgs] == before


def test_one_round_past_the_gate_collapses_everything_but_the_keep_window():
    rounds = LEDGER_KEEP_ROUNDS + LEDGER_SLACK_ROUNDS + 1
    msgs = _transcript(rounds)
    stats = compact_tool_exchanges(msgs)

    assert stats["groups"] == rounds - LEDGER_KEEP_ROUNDS
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    collapsed = [m for m in tool_msgs if "Execution ledger" in m["content"]]
    assert len(collapsed) == rounds - LEDGER_KEEP_ROUNDS
    # The most recent exchanges are untouched — that is where an exact
    # read_file body is still needed by the edit_file that follows it.
    for msg in tool_msgs[-LEDGER_KEEP_ROUNDS:]:
        assert "Execution ledger" not in msg["content"]


def test_a_batch_does_not_fire_again_on_the_next_round():
    """The invalidation cadence, which is the whole prefix-stability argument.

    After a batch, the gate must fall back below its threshold. It only does so
    if every exchange the batch walked is marked processed — including ones it
    deliberately left verbatim — so this also pins that behaviour.
    """
    msgs = _transcript(LEDGER_KEEP_ROUNDS + LEDGER_SLACK_ROUNDS + 1)
    assert compact_tool_exchanges(msgs)["entries"] > 0

    # A batch leaves `keep` exchanges unprocessed, and the gate needs
    # `keep + slack + 1` before it fires again: one batch per slack + 1 rounds.
    fired = []
    for i in range(LEDGER_SLACK_ROUNDS + 1):
        msgs.extend(_native_round(100 + i, _read_result(f"/srv/app/later_{i}.py")))
        fired.append(compact_tool_exchanges(msgs)["entries"] > 0)
    assert fired.count(True) == 1
    assert fired[-1] is True


def test_short_results_are_never_collapsed():
    """Small results are ids and confirmations: all fact, no bulk."""
    msgs = _transcript(0)
    for i in range(LEDGER_KEEP_ROUNDS + LEDGER_SLACK_ROUNDS + 2):
        msgs.extend(_native_round(i, f"### write_file: /tmp/f{i}\nFile written: /tmp/f{i} (12 bytes)"))
    stats = compact_tool_exchanges(msgs)
    assert stats["entries"] == 0
    assert all(len(m["content"]) < LEDGER_MIN_RESULT_CHARS for m in msgs if m.get("role") == "tool")


# ── What survives ───────────────────────────────────────────────────────────

def test_the_entry_keeps_the_path_and_drops_the_bulk():
    msgs = _transcript(LEDGER_KEEP_ROUNDS + LEDGER_SLACK_ROUNDS + 1)
    compact_tool_exchanges(msgs)
    entry = next(m["content"] for m in msgs if m.get("role") == "tool" and m.get(LEDGER_MARK))

    assert "### read_file: /srv/app/src/module_0.py" in entry  # the fact
    assert "x line 200" not in entry                           # the bulk
    assert "chars" in entry and "omitted" in entry             # and it says so


def test_every_collapsed_result_is_recoverable_verbatim():
    import src.tool_output_store as tos

    msgs = _transcript(LEDGER_KEEP_ROUNDS + LEDGER_SLACK_ROUNDS + 1)
    originals = {
        id(m): m["content"] for m in msgs
        if m.get("role") == "tool"
    }
    compact_tool_exchanges(msgs)

    collapsed = [m for m in msgs if m.get("role") == "tool" and m.get(LEDGER_MARK)]
    assert collapsed
    for msg in collapsed:
        refs = tos._REF_RE.findall(msg["content"])
        assert refs, f"no recallable ref in: {msg['content'][:200]}"
        assert tos.load(refs[0]) == originals[id(msg)]


def test_an_existing_offload_ref_is_carried_forward():
    """A result that was already offloaded must keep pointing at its own text."""
    excerpt = (
        "### bash: cat /var/log/app.log\n```\nhead of log\n```\n"
        "[... 58,000 of 60,000 characters held outside the conversation as "
        "`toolout-abcdef0123` ...]\n"
        "This output was large, so only its head and tail are shown. "
        + "padding. " * 60
    )
    msgs = _transcript(0)
    for i in range(LEDGER_KEEP_ROUNDS + LEDGER_SLACK_ROUNDS + 1):
        msgs.extend(_native_round(i, excerpt))
    compact_tool_exchanges(msgs)

    entry = next(m["content"] for m in msgs if m.get("role") == "tool" and m.get(LEDGER_MARK))
    assert "toolout-abcdef0123" in entry
    assert "recall_tool_output" in entry


def test_ids_and_versions_outside_fences_survive():
    result = (
        '### create_document: Quarterly plan\n'
        'Document created: "Quarterly plan" (id: doc_7f3a91, v1)\n'
        "**data:**\n```json\n" + '{"body": "' + "z" * 5000 + '"}\n```'
    )
    entry = ledger_entry(result, ["toolout-1111111111"])
    assert "doc_7f3a91" in entry
    assert "v1" in entry
    assert "zzzz" not in entry


# ── What must not be collapsed ──────────────────────────────────────────────

def test_an_unresolved_failure_stays_verbatim():
    msgs = _transcript(0)
    failing = _bash_result("pytest tests/test_thing.py", exit_code=1)
    msgs.extend(_native_round(0, failing, tool="bash"))
    for i in range(1, LEDGER_KEEP_ROUNDS + LEDGER_SLACK_ROUNDS + 2):
        msgs.extend(_native_round(i, _read_result(f"/srv/app/m{i}.py")))
    compact_tool_exchanges(msgs)

    first = next(m for m in msgs if m.get("role") == "tool")
    assert "Execution ledger" not in first["content"]
    assert first["content"] == failing


def test_a_failure_resolved_by_a_later_success_may_collapse_but_keeps_the_error():
    msgs = _transcript(0)
    msgs.extend(_native_round(0, _bash_result("pytest -q", exit_code=1), tool="bash"))
    for i in range(1, LEDGER_KEEP_ROUNDS + LEDGER_SLACK_ROUNDS + 2):
        msgs.extend(_native_round(i, _bash_result(f"echo step {i}"), tool="bash"))
    compact_tool_exchanges(msgs)

    first = next(m for m in msgs if m.get("role") == "tool")
    assert "Execution ledger" in first["content"]
    # The failure is still legible, so the agent does not retry it blind.
    assert "### bash: pytest -q" in first["content"]
    assert "**exit_code:** 1" in first["content"]


def test_a_deferred_failure_is_reconsidered_once_a_later_round_resolves_it():
    """Deferral, not permanent exemption — and without re-arming the batch gate.

    A failing test run that is fixed three rounds later should not ride along
    for the rest of the turn, but revisiting it must not make a batch fire every
    round either. The mark distinguishes "deferred" from "finalized" so both
    hold.
    """
    msgs = _transcript(0)
    msgs.extend(_native_round(0, _bash_result("pytest -q", exit_code=1), tool="bash"))
    for i in range(1, LEDGER_KEEP_ROUNDS + LEDGER_SLACK_ROUNDS + 2):
        msgs.extend(_native_round(i, _read_result(f"/srv/app/m{i}.py"), tool="read_file"))
    compact_tool_exchanges(msgs)

    first = next(m for m in msgs if m.get("role") == "tool")
    assert first[LEDGER_MARK] == "deferred"
    assert "Execution ledger" not in first["content"]

    fired = []
    for i in range(100, 100 + LEDGER_SLACK_ROUNDS + 1):
        tool = "bash" if i == 100 else "read_file"  # the round that fixes it
        text = _bash_result("pytest -q") if i == 100 else _read_result(f"/srv/app/n{i}.py")
        msgs.extend(_native_round(i, text, tool=tool))
        fired.append(compact_tool_exchanges(msgs)["entries"] > 0)

    assert fired.count(True) == 1  # still one batch per window
    assert "Execution ledger" in first["content"]
    assert "**exit_code:** 1" in first["content"]
    assert first[LEDGER_MARK] is True


def test_recall_results_are_never_collapsed():
    """Collapsing a recall invites the re-ask loop tool_output_store documents."""
    recall = "### recall_tool_output\n" + "the slice the model asked for. " * 60
    msgs = _transcript(0)
    for i in range(LEDGER_KEEP_ROUNDS + LEDGER_SLACK_ROUNDS + 2):
        msgs.extend(_native_round(i, recall, tool="recall_tool_output"))
    compact_tool_exchanges(msgs)
    assert all(
        m["content"] == recall for m in msgs if m.get("role") == "tool"
    )


def test_registry_dispatched_recall_results_are_never_collapsed():
    """The real header: execute_tool_block routes recall through the registry,
    so its desc is `registry: recall_tool_output {...}`. Reading "registry" as
    the tool name compacted recalled slices and the agent recalled them again."""
    recall = (
        '### registry: recall_tool_output {"ref": "toolout-b3b8db60cf", "query": "state"}\n'
        + "the slice the model asked for. " * 60
    )
    msgs = _transcript(0)
    for i in range(LEDGER_KEEP_ROUNDS + LEDGER_SLACK_ROUNDS + 2):
        msgs.extend(_native_round(i, recall, tool="recall_tool_output"))
    compact_tool_exchanges(msgs)
    assert all(
        m["content"] == recall for m in msgs if m.get("role") == "tool"
    )


def test_dispatch_prefix_is_not_the_tool_name():
    from src.context_compactor import _ledger_tool_name

    assert _ledger_tool_name('### registry: recall_tool_output {"ref": "x"}') == "recall_tool_output"
    assert _ledger_tool_name("### mcp: mcp__github_read__issue_read") == "mcp__github_read__issue_read"
    assert _ledger_tool_name("### read_file: /srv/app.py") == "read_file"
    assert _ledger_tool_name("### bash") == "bash"


def test_a_failed_store_leaves_the_exchange_untouched(monkeypatch):
    """Offload is the precondition for compacting, not a bonus."""
    import src.tool_output_store as tos

    monkeypatch.setattr(tos, "store", lambda *a, **k: None)
    msgs = _transcript(LEDGER_KEEP_ROUNDS + LEDGER_SLACK_ROUNDS + 1)
    before = [m.get("content") for m in msgs]
    stats = compact_tool_exchanges(msgs)
    assert stats["entries"] == 0
    assert [m.get("content") for m in msgs] == before


def test_the_kill_switch_disables_it(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_AGENT_EXECUTION_LEDGER", "0")
    msgs = _transcript(LEDGER_KEEP_ROUNDS + LEDGER_SLACK_ROUNDS + 4)
    assert compact_tool_exchanges(msgs)["entries"] == 0


# ── Structure the providers enforce ─────────────────────────────────────────

def test_message_structure_is_unchanged():
    """Only result CONTENT is rewritten; tool_calls/tool pairing is untouched."""
    msgs = _transcript(LEDGER_KEEP_ROUNDS + LEDGER_SLACK_ROUNDS + 1)
    shape = [(m.get("role"), bool(m.get("tool_calls")), m.get("tool_call_id")) for m in msgs]
    compact_tool_exchanges(msgs)
    assert [(m.get("role"), bool(m.get("tool_calls")), m.get("tool_call_id")) for m in msgs] == shape


def test_the_internal_mark_never_reaches_a_provider():
    from src.llm_core import _sanitize_llm_messages

    msgs = _transcript(LEDGER_KEEP_ROUNDS + LEDGER_SLACK_ROUNDS + 1)
    compact_tool_exchanges(msgs)
    assert any(m.get(LEDGER_MARK) for m in msgs)
    assert all(LEDGER_MARK not in m for m in _sanitize_llm_messages(msgs))


def test_the_tool_name_is_found_behind_the_untrusted_wrapper():
    """A textual result sits behind ~480 chars of injection guard.

    Reading the tool name only off the first line made every textual-path result
    anonymous, which silently disabled failure-resolution for that whole
    transcript shape: a fixed `pytest` run rode along verbatim to the end of the
    turn.
    """
    from src.context_compactor import _ledger_tool_name

    wrapped = untrusted_context_message(
        "tool execution results", _bash_result("pytest -q", exit_code=1),
    )
    assert _ledger_tool_name(wrapped["content"]) == "bash"
    # And through a second pass, once the result is already a ledger entry.
    assert _ledger_tool_name(ledger_entry(wrapped["content"], ["toolout-0000000000"])) == "bash"


def test_textual_transcripts_are_handled_too():
    msgs = _transcript(LEDGER_KEEP_ROUNDS + LEDGER_SLACK_ROUNDS + 1, textual=True)
    stats = compact_tool_exchanges(msgs)
    assert stats["entries"] == stats["groups"] == LEDGER_SLACK_ROUNDS + 1
    from src.prompt_security import (
        GUARD_CLOSE, GUARD_OPEN, UNTRUSTED_CONTEXT_HEADER,
    )

    guarded = [m for m in msgs if (m.get("metadata") or {}).get("source") == "tool execution results"]
    collapsed = [m for m in guarded if m.get(LEDGER_MARK)]
    assert collapsed
    for msg in collapsed:
        # Still user-role untrusted data with its fence reproduced byte for
        # byte: the ledger rewrites the body, never the framing that makes tool
        # output data rather than instructions. Truncating that warning
        # mid-sentence would be a quiet prompt-injection regression.
        assert msg["role"] == "user"
        assert msg["content"].startswith(UNTRUSTED_CONTEXT_HEADER)
        assert GUARD_OPEN in msg["content"]
        assert msg["content"].rstrip().endswith(GUARD_CLOSE)
        assert "Source: tool execution results" in msg["content"]
        assert "Execution ledger" in msg["content"]


def test_the_agent_loop_actually_calls_it():
    """The wiring, not just the mechanism: _append_tool_results is the one caller.

    Driven through both transcript shapes because `_append_tool_results` builds
    a different one for native tool_calls than for textual tool blocks, and a
    ledger that only understood one of them would silently no-op on half the
    fleet.
    """
    from src.agent_loop import _append_tool_results

    for used_native in (True, False):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        sizes = []
        for i in range(LEDGER_KEEP_ROUNDS + LEDGER_SLACK_ROUNDS + 2):
            body = _read_result(f"/srv/app/f{i}.py", 6000, seed=f"s{i}")
            calls = [{"id": f"c{i}", "name": "read_file", "arguments": "{}"}]
            _append_tool_results(
                msgs, "reading", calls if used_native else [],
                [body], [body], used_native, i,
            )
            sizes.append(estimate_tokens(msgs))
        # Monotonic growth is what the audit found; the ledger must break it.
        assert min(sizes[1:]) < max(sizes) / 1.5
        assert any(m.get(LEDGER_MARK) for m in msgs)


# ── The measurement ─────────────────────────────────────────────────────────

def measure_26_round_turn(textual=False):
    """Prompt tokens per round for a reconstructed 26-round coding turn.

    Shaped after the audited workflow: file reads, greps, test runs and edits,
    most of them a few thousand characters, one failing test run that stays
    unresolved for a while. Returns (before, after) token series.
    """
    scripts = [
        ("read_file", lambda i: _read_result(f"/srv/app/src/agent_loop.py", 9000, seed=f"r{i}")),
        ("bash", lambda i: _bash_result(f"grep -rn pattern{i} src/", 5000)),
        ("bash", lambda i: _bash_result(f"python -m pytest tests/test_{i}.py", 7000)),
        ("read_file", lambda i: _read_result(f"/srv/app/src/context_compactor.py", 6000, seed=f"c{i}")),
        ("edit_file", lambda i: f"### edit_file: patched src/mod_{i}.py\n```\n"
                                + ("- old\n+ new\n" * 120) + "```"),
    ]
    base = [
        {"role": "system", "content": "You are a coding agent. " * 200},
        {"role": "user", "content": "Add a ledger to the agent loop and measure it."},
    ]
    plain = list(base)
    ledgered = list(base)
    before, after = [], []
    for i in range(26):
        tool, make = scripts[i % len(scripts)]
        text = make(i)
        if i == 6:  # one failing run that stays unresolved for several rounds
            text = _bash_result("python -m pytest -q", 7000, exit_code=1)
            tool = "bash"
        rnd = _textual_round if textual else (lambda t, i=i, tool=tool: _native_round(i, t, tool))
        plain.extend(rnd(text))
        ledgered.extend(rnd(text))
        compact_tool_exchanges(ledgered)
        before.append(estimate_tokens(plain))
        after.append(estimate_tokens(ledgered))
    return before, after


def test_a_26_round_turn_gets_materially_cheaper():
    before, after = measure_26_round_turn()
    # Early rounds are identical — the gate has not fired yet.
    assert before[:LEDGER_KEEP_ROUNDS + LEDGER_SLACK_ROUNDS] == after[:LEDGER_KEEP_ROUNDS + LEDGER_SLACK_ROUNDS]
    # By the end of the turn the prompt is far smaller, and the *growth* per
    # round is what actually flattens.
    assert after[-1] < before[-1] * 0.45
    assert sum(after) < sum(before) * 0.6
