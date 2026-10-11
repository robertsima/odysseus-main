"""The Agents overview shows a blocked or partly done worker as such, and as needing you when it says so.

A worker whose report said `Outcome: blocked` was listed as Finished, and the
one thing the person had to do sat inside a collapsed hand-back.
"""
import pytest

from src import agent_activity as act
from tests.core.database.test_agents_dashboard_routes import _Sess, _req, env  # noqa: F401


def _finished_worker(mgr, status, **summary):
    mgr.sessions["w1"] = _Sess("w1", "Builder")
    run_id = act.run_started("w1", "session", "Worker \u00b7 build", owner="alice",
                             data={"target_session": "w1", "parent_session": "a1", "mode": "agent"})
    act.run_finished("w1", "session", run_id, "Worker done", status=status, owner="alice", data=summary)
    return run_id


async def _row(eps, sid="w1"):
    out = await eps[("GET", "/api/agents/overview")](_req(), current_session="a1")
    return next(r for r in out["rows"] if r["session_id"] == sid), out


async def test_a_blocked_worker_is_listed_as_blocked_with_what_it_needs(env):
    mgr, eps = env
    _finished_worker(mgr, "blocked", needs_user=["paste the staging key"])

    row, out = await _row(eps)

    assert row["status"] == "blocked" and row["needs"] == ["paste the staging key"]
    assert out["totals"]["needs_input"] == 1


async def test_a_partial_worker_is_listed_as_partial_and_asks_nothing_without_a_needs_line(env):
    mgr, eps = env
    _finished_worker(mgr, "partial", open_first="wire the retry path")

    row, out = await _row(eps)

    assert row["status"] == "partial" and row["needs"] == []
    assert out["totals"]["needs_input"] == 0


async def test_a_completed_worker_is_still_finished(env):
    mgr, eps = env
    _finished_worker(mgr, "completed", verification="unverified")

    row, _ = await _row(eps)

    assert row["status"] == "finished"
