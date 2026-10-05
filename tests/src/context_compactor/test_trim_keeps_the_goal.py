"""Front-trimming must not throw away the request the run is serving.

`trim_for_context` protects `convo_msgs[-1]` as "the current turn", but during
an agent run the last message is a TOOL RESULT — the user's request is further
back, and popping from the front reaches it first. A long run therefore kept the
most recent log dump and lost what it was supposed to do with it.
"""
from src.context_compactor import TRIM_TARGET_RATIO, trim_for_context


GOAL = "Audit every invoice in the vault and write the totals into Rolling Report."


def _agent_run(rounds: int, result_chars: int = 4000):
    """A realistic mid-run transcript: request, then N tool round-trips."""
    messages = [
        {"role": "system", "content": "You are Odysseus."},
        {"role": "user", "content": GOAL},
    ]
    for i in range(rounds):
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": "bash", "arguments": '{"cmd":"ls"}'},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": f"call_{i}",
            "content": f"result {i}: " + ("x" * result_chars),
        })
    return messages


def _texts(messages):
    return "\n".join(str(m.get("content", "")) for m in messages)


def test_the_request_survives_a_long_tool_run():
    trimmed = trim_for_context(_agent_run(30), context_length=8000, reserve_tokens=512)
    assert GOAL in _texts(trimmed), (
        "the goal was trimmed away while tool output was kept"
    )


def test_recent_tool_output_is_still_preferred_over_old():
    trimmed = trim_for_context(_agent_run(30), context_length=8000, reserve_tokens=512)
    text = _texts(trimmed)
    assert "result 29" in text, "the newest result must survive"
    assert "result 0" not in text, "the oldest result should be the first to go"


def test_trimming_cuts_below_the_budget_so_it_does_not_refire_every_round():
    """Trimming rewrites the prompt prefix, which costs a full re-prefill.
    Stopping exactly at the budget means the next round is over again."""
    from src.model_context import estimate_tokens

    context_length, reserve = 8000, 512
    trimmed = trim_for_context(_agent_run(30), context_length, reserve)
    budget = context_length - reserve

    assert estimate_tokens(trimmed) <= budget * TRIM_TARGET_RATIO + 400, (
        "trim stopped at the edge of the budget instead of cutting below it"
    )


def test_a_conversation_that_fits_is_returned_untouched():
    messages = _agent_run(1, result_chars=50)
    assert trim_for_context(messages, context_length=100_000) is messages


def test_an_enormous_request_is_shortened_not_dropped():
    messages = _agent_run(20)
    messages[1] = {"role": "user", "content": "GOAL-MARKER " + ("y" * 60_000)}

    trimmed = trim_for_context(messages, context_length=8000, reserve_tokens=512)
    text = _texts(trimmed)
    assert "GOAL-MARKER" in text, "the request must survive in some form"
    assert len(text) < 60_000, "an oversized request must be shortened to fit"


def test_tool_pairing_survives_the_reordering():
    """Re-inserting the anchor ahead of the survivors can orphan a tool message;
    every `tool` must still follow an assistant turn that called it."""
    trimmed = trim_for_context(_agent_run(30), context_length=8000, reserve_tokens=512)

    open_ids = set()
    for m in trimmed:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            open_ids = {c["id"] for c in m["tool_calls"]}
        elif m.get("role") == "tool":
            assert m["tool_call_id"] in open_ids, (
                f"orphaned tool message {m['tool_call_id']}"
            )
