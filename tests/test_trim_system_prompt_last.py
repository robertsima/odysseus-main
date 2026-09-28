"""A context trim cuts old history first and the system prompt only as a last resort.

Production, 2026-09-28 (admin chat on gpt-6-luna, ChatGPT Codex Responses,
200k input budget): a round at 201k tokens against a 198,976 budget was fitted
by cutting the system prompt to 2000 characters -- the loadout instructions,
tool rules and safety policy gone -- and returned without touching the
history. The next round was trimmed by history (to 119k), and the round after
that fit again, so the full prompt came back. On a Responses route every system
message is merged into `instructions`, the first bytes of the cached prefix, so
each of those three rounds re-billed the whole request (4%, 7%, 8% cached), and
13 rounds that hour ran on the 2045-character stub.
"""
import logging

from src.agent_loop import _AGENT_TRIM_TARGET_RATIO, _sticky_trim
from src.context_compactor import (
    SYSTEM_TRUNCATION_MARKER,
    TRIM_TARGET_RATIO,
    trim_for_context,
    truncate_system_message,
)
from src.llm_core import _build_chatgpt_responses_payload
from src.model_context import estimate_tokens

SYSTEM = "Prompt-safety policy. " + "Operating rule: follow the loadout instructions. " * 190  # ~9.5k chars
EXTRA = {"role": "system", "content": "[Conversation summary — earlier messages were compacted]\n" + "s" * 800}


def _chat(turns: int, size: int = 2000):
    """A long chat: system prompt, then user/assistant turns."""
    msgs = [{"role": "system", "content": SYSTEM}, dict(EXTRA)]
    for i in range(turns):
        msgs.append({"role": "user", "content": f"question {i} " + "q" * size})
        msgs.append({"role": "assistant", "content": f"answer {i} " + "a" * size})
    return msgs


def _budget_for(msgs, over: float):
    """A context length the messages exceed by `over` (e.g. 0.01 = 1%)."""
    return int(estimate_tokens(msgs) / (1 + over)) + 512


def _system_texts(msgs):
    return [m["content"] for m in msgs if m.get("role") == "system"]


def _convo(msgs):
    return [m for m in msgs if m.get("role") != "system"]


def test_a_small_overshoot_trims_history_not_the_system_prompt(caplog):
    msgs = _chat(60) + [{"role": "user", "content": "latest request"}]
    ctx = _budget_for(msgs, 0.01)
    with caplog.at_level(logging.INFO, logger="src.context_compactor"):
        trimmed = trim_for_context(msgs, ctx)

    # Every system message survives, byte for byte and in its order.
    assert _system_texts(trimmed) == [SYSTEM, EXTRA["content"]]
    assert not any(SYSTEM_TRUNCATION_MARKER in t for t in _system_texts(trimmed))
    # History was cut, below the budget (hysteresis), and the request survives.
    assert len(_convo(trimmed)) < len(_convo(msgs))
    assert estimate_tokens(trimmed) <= (ctx - 512) * TRIM_TARGET_RATIO
    assert trimmed[-1]["content"] == "latest request"
    # The trim says what it removed, in counts and sizes -- never content.
    line = next(r.getMessage() for r in caplog.records if "[context-trim]" in r.getMessage())
    assert "history_dropped=" in line and "system_truncated" not in line
    assert "question" not in line and "Operating rule" not in line


def test_the_system_prompt_is_cut_only_once_history_is_at_its_minimum(caplog):
    # Ten short recent messages + the request are the history's minimum; the
    # system prompt alone is over budget.
    msgs = [{"role": "system", "content": SYSTEM}, dict(EXTRA)] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(12)
    ] + [{"role": "user", "content": "latest request"}]
    with caplog.at_level(logging.INFO, logger="src.context_compactor"):
        trimmed = trim_for_context(msgs, 2400, reserve_tokens=512)
    systems = _system_texts(trimmed)
    # The extra system message goes first, then the leading prompt is cut.
    assert systems == [SYSTEM[:2000] + SYSTEM_TRUNCATION_MARKER]
    line = next(r.getMessage() for r in caplog.records if "[context-trim]" in r.getMessage())
    assert "system_dropped=1msgs" in line and "system_truncated=" in line
    assert trimmed[-1]["content"] == "latest request"


def test_the_system_cut_is_deterministic_and_idempotent():
    msg = {"role": "system", "content": SYSTEM, "_agent_injected": "merged_prompt"}
    once = truncate_system_message(msg)
    assert once["content"] == SYSTEM[:2000] + SYSTEM_TRUNCATION_MARKER
    assert once["_agent_injected"] == "merged_prompt"
    assert truncate_system_message(once) is once
    assert truncate_system_message(dict(msg))["content"] == once["content"]


def test_survivors_keep_their_order_and_the_anchor_stays_in_place():
    """Moving the last user message to the front of what survived put a
    different message at the head of the conversation on every turn."""
    msgs = _chat(60)
    msgs += [{"role": "user", "content": "latest request"}]
    trimmed = trim_for_context(msgs, _budget_for(msgs, 0.05))
    kept = _convo(trimmed)
    # The kept conversation is a contiguous tail of the original, in order.
    original = _convo(msgs)
    start = original.index(kept[0])
    assert kept == original[start:]
    # A protected message (the open document) is not moved either.
    doc = {"role": "user", "content": "ACTIVE DOCUMENT", "_protected": True}
    with_doc = msgs[:-1] + [doc] + msgs[-1:]
    trimmed = trim_for_context(with_doc, _budget_for(with_doc, 0.05))
    assert trimmed[-2] is doc and trimmed[-1]["content"] == "latest request"


def test_the_next_turns_fresh_trim_lands_on_the_same_cut():
    """Each turn's first request is trimmed from scratch; the persisted history
    only grew at its end, so the cut must stay put for many turns (whole
    grains), keeping the conversation's cached prefix."""
    base = _chat(60)
    ctx = _budget_for(base, 0.02)
    heads, moves = [], 0
    history = list(base)
    for turn in range(12):
        history = history + [
            {"role": "user", "content": f"new question {turn} " + "n" * 600},
            {"role": "assistant", "content": f"new answer {turn} " + "n" * 600},
        ]
        request = history + [{"role": "user", "content": f"request {turn}"}]
        kept = _convo(trim_for_context(request, ctx))
        heads.append(kept[0]["content"][:20])
    moves = sum(1 for a, b in zip(heads, heads[1:]) if a != b)
    assert moves <= 3, heads


def test_an_agent_turn_keeps_full_instructions_on_every_round():
    """The 2026-09-28 pattern end to end: rounds of one turn, each over the
    budget, built into the Codex payload. The instructions never change and
    are never the truncated stub."""
    system = {"role": "system", "content": SYSTEM, "_agent_injected": "merged_prompt"}
    history = [system] + _convo(_chat(55)) + [{"role": "user", "content": "do the audit"}]
    ctx = _budget_for(history, -0.01)  # just under: the first rounds fit
    state = {}
    instructions = []
    for i in range(30):
        history.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]})
        history.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 1500})
        req = _sticky_trim(state, ("u", "m"), history, lambda msgs: trim_for_context(
            msgs, ctx, reserve_tokens=512, target_ratio=_AGENT_TRIM_TARGET_RATIO))
        assert estimate_tokens(req) <= ctx - 512
        payload = _build_chatgpt_responses_payload("gpt-6-luna", req, 0.3, 4096)
        instructions.append(payload["instructions"])
    assert set(instructions) == {SYSTEM.strip()}


def test_a_system_cut_stays_for_the_rest_of_the_turn():
    """When the cut is unavoidable, later rounds that would fit again keep it:
    flipping back to the full prompt re-bills the request twice."""
    system = {"role": "system", "content": SYSTEM, "_agent_injected": "merged_prompt"}
    request = {"role": "user", "content": "do it"}
    history = [system, request]
    state = {}
    budget_ctx = 2400
    sent = []
    for i in range(12):
        history.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "grep", "arguments": "{}"}}]})
        history.append({"role": "tool", "tool_call_id": f"c{i}", "content": "r" * 40})
        req = _sticky_trim(state, ("u", "m"), history,
                           lambda msgs: trim_for_context(msgs, budget_ctx, reserve_tokens=512))
        sent.append(next(m["content"] for m in req if m.get("role") == "system"))
    first_cut = next(i for i, text in enumerate(sent) if text.endswith(SYSTEM_TRUNCATION_MARKER))
    assert all(text == sent[first_cut] for text in sent[first_cut:])
