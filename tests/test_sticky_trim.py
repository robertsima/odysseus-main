"""The agent's per-round trim keeps its cut instead of re-cutting every round.

The loop's history only grows, and each round's request used to be trimmed
from scratch. The trim stops as soon as the request fits, so its cut point
moved forward every round. The prompt's start then changed every round and
the provider's prefix cache missed on all of them (a research turn
re-prefilled ~150k tokens per round).
"""
from src.agent_loop import _AGENT_TRIM_TARGET_RATIO, _sticky_trim
from src.context_compactor import trim_for_context

BUDGET = 60_000  # a round is ~5% of it, as in a real 200k-budget research turn


def _round(i, size=2400):
    call = {"role": "assistant", "content": None,
            "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "search", "arguments": "{}"}}]}
    result = {"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i} " + ("x" * size * 4)}
    return [call, result]


def _run(sticky: bool, rounds=60):
    system = {"role": "system", "content": "You are an agent. " * 50}
    request = {"role": "user", "content": "Research the issue and report."}
    history = [system, request]
    state = {}
    requests = []
    for i in range(rounds):
        history.extend(_round(i))

        def trim(msgs):
            return trim_for_context(msgs, BUDGET, reserve_tokens=512, target_ratio=_AGENT_TRIM_TARGET_RATIO)

        req = _sticky_trim(state, ("u", "m"), history, trim) if sticky else trim(history)
        requests.append(list(req))
    return requests


def _cache_breaks(requests):
    """Rounds whose request does not start with the previous round's request."""
    breaks = 0
    for prev, cur in zip(requests, requests[1:]):
        prefix = [m.get("content") for m in prev]
        if [m.get("content") for m in cur[:len(prev)]] != prefix:
            breaks += 1
    return breaks


def test_sticky_trim_breaks_the_cached_prefix_far_less_often(monkeypatch):
    import src.context_compactor as compactor

    # A cut that stops at the smallest removal (no grains): every round once
    # over budget moves it.
    monkeypatch.setattr(compactor, "TRIM_GRAIN_RATIO", 1e-9)
    plain = _run(False)
    assert _cache_breaks(plain) >= 20
    monkeypatch.undo()
    # From-scratch trims cut in whole grains, so they already move the cut
    # only every few rounds; remembering the cut does better still.
    grained, sticky = _run(False), _run(True)
    assert _cache_breaks(grained) <= _cache_breaks(plain) // 2
    assert _cache_breaks(sticky) <= _cache_breaks(plain) // 4
    assert _cache_breaks(sticky) < _cache_breaks(grained)


def test_every_request_still_fits_and_keeps_the_system_prompt_and_request():
    for req in _run(True):
        assert req[0]["role"] == "system" and req[0]["content"].startswith("You are an agent.")
        assert any(m.get("content") == "Research the issue and report." for m in req)
        assert sum(len(str(m.get("content") or "")) for m in req) // 4 <= BUDGET
        # tool results always follow the assistant turn that called them
        ids = set()
        for m in req:
            if m.get("role") == "assistant":
                ids |= {c["id"] for c in m.get("tool_calls") or []}
            if m.get("role") == "tool":
                assert m["tool_call_id"] in ids


def test_under_budget_nothing_is_touched():
    history = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}] + _round(0, size=10)
    state = {}
    assert _sticky_trim(state, ("u", "m"), history, lambda msgs: trim_for_context(msgs, BUDGET)) is history
    assert state == {}


def test_a_cut_is_noted_where_it_is_and_the_note_stays_put():
    """A trim used to drop history silently. The note goes on the first kept
    message after the gap, the same one every round while the cut stays, and
    the user's request is left as written."""
    requests = _run(True, rounds=40)
    noted = [r for r in requests if any("left out of the context here" in str(m.get("content")) for m in r)]
    assert noted, "a turn this long must have been cut"
    for req in noted:
        notes = [m for m in req if "left out of the context here" in str(m.get("content"))]
        assert len(notes) == 1 and "recall_chat_history" in notes[0]["content"]
        assert any(m.get("content") == "Research the issue and report." for m in req)
    # Between cuts the note does not move, so the cached prefix holds.
    assert _cache_breaks(requests) <= _cache_breaks(_run(False, rounds=40))
