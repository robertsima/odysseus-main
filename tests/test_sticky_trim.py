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


def test_sticky_trim_breaks_the_cached_prefix_far_less_often():
    plain, sticky = _run(False), _run(True)
    assert _cache_breaks(plain) >= 20        # every round once over budget
    assert _cache_breaks(sticky) <= _cache_breaks(plain) // 4


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
