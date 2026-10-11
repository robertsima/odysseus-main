"""A launch refused for capacity shows on the outcome the calling chat serves.

The refusal used to live only in the tool's error text; the outcome kept saying
it was moving. The tool, the store and the blocker builder are real.
"""
import pytest

from src import agent_loadouts
from src import outcomes as store
from src.agent_tools.loadout_tools import manage_agent_loadout


@pytest.fixture
def serving(monkeypatch, app_db):
    import core.database as cdb

    monkeypatch.setattr(cdb, "SessionLocal", app_db.SessionLocal)
    out = store.create("u", "Ship the parser", links=[{"kind": "chat", "ref": "c"}])["outcome"]
    store.set_state("u", out["id"], "active", actor="person")
    return out["id"]


def _policy(limit):
    known = {"bash", "read_file"}
    return {"allowed_tools": set(known), "known_tools": set(known), "skill_names": set(), "allowed_models": set(),
            "memory_access": "write", "skill_access": "all", "model_access": "all", "allowed_mcp_servers": ["*"],
            "private_vault_access": True, "delegation_policy": "auto", "max_parallel_workers": limit,
            "approval_mode": "auto"}


async def test_start_at_the_worker_limit_blocks_the_outcome_as_auto_recovery(monkeypatch, serving):
    monkeypatch.setattr(agent_loadouts, "caller_policy", lambda sid, owner: _policy(1))
    monkeypatch.setattr("src.agent_control.live_children", lambda sid: 1)

    result = await manage_agent_loadout('{"action": "start", "task": "go"}', "c", owner="u")

    assert result["blocked_reason"] == "worker_capacity"
    got = store.get("u", serving)
    assert got["state"] == "blocked"
    assert got["blocker"]["code"] == "WORKER_CAPACITY" and got["blocker"]["recovery"] == "auto"


async def test_start_in_a_chat_that_allows_no_workers_needs_the_person(monkeypatch, serving):
    monkeypatch.setattr(agent_loadouts, "caller_policy", lambda sid, owner: _policy(0))
    monkeypatch.setattr("src.agent_control.live_children", lambda sid: 0)

    await manage_agent_loadout('{"action": "start", "task": "go"}', "c", owner="u")

    assert store.get("u", serving)["blocker"]["recovery"] == "user"
