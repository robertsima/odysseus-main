"""Why the agent stopped unblocking itself (2026-09-18), pinned.

c62b6ba routed every late tool addition through one cumulative budget
(`_admit_turn_tools`: 32 tools / 5000 schema tokens) with the turn's existing
selection entered as protected `core`. The recovery paths entered as
unprotected sources, so once a turn's first guesses filled the budget the
self-unblock, `discover_tools` and a loaded skill's tools all admitted nothing
-- silently, with the model told the tools had loaded.

Separately, a continuation turn ("Ok, continue") could not carry the previous
turn's tools: persisted turns replay `metadata.tool_events`, never
`tool_calls`, so retention saw a native-tool chat as never having used a tool.
"""

import pytest

from src.tool_selection import PROTECTED_SOURCES, plan_tool_selection


# ── the budget: named requests get past it, guesses do not ───────────────────


def _full_turn():
    """A turn whose existing selection already spends the whole budget."""
    existing = {f"guess_{i}" for i in range(20)}
    costs = {name: 250 for name in existing}          # 20 x 250 = 5000 tokens
    costs["manage_git"] = 900
    return existing, costs


@pytest.mark.parametrize("source", ["explicit", "skill"])
def test_a_named_request_is_admitted_past_a_full_budget(source):
    existing, costs = _full_turn()
    plan = plan_tool_selection(
        existing | {"manage_git"}, {"core": existing, source: {"manage_git"}},
        schema_costs=costs, max_tools=32, max_schema_tokens=5000,
    )
    assert "manage_git" in plan.selected
    assert source in PROTECTED_SOURCES


@pytest.mark.parametrize("source", ["semantic", "context"])
def test_a_guess_is_still_refused_on_a_full_budget(source):
    """The budget still does its job for everything that is not a named request."""
    existing, costs = _full_turn()
    plan = plan_tool_selection(
        existing | {"manage_git"}, {"core": existing, source: {"manage_git"}},
        schema_costs=costs, max_tools=32, max_schema_tokens=5000,
    )
    assert "manage_git" not in plan.selected
