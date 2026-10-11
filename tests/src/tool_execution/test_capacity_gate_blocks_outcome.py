"""The tool gate that holds a chat to its worker limit also marks the outcome it serves."""
from src import outcomes as store


def _no_security_context():
    import src.tool_execution as tool_execution

    return tool_execution.NO_TOOL_SECURITY_CONTEXT


async def test_a_delegation_refused_at_the_limit_blocks_the_outcome_as_auto_recovery(monkeypatch, app_db):
    import core.database as cdb
    from src import agent_control
    from src.tool_execution import execute_tool_block
    from src.tool_types import ToolBlock

    monkeypatch.setattr(cdb, "SessionLocal", app_db.SessionLocal)
    monkeypatch.setattr(cdb, "get_session_settings", lambda sid, **kw: {})
    monkeypatch.setattr(agent_control, "live_children", lambda sid: 1)
    out = store.create("alice", "Audit the repo", links=[{"kind": "chat", "ref": "chat-1"}])["outcome"]
    store.set_state("alice", out["id"], "active", actor="person")

    desc, result = await execute_tool_block(
        ToolBlock("delegate_to_agent", '{"action":"start","prompt":"audit"}'), session_id="chat-1",
        owner="alice", security_context=_no_security_context())

    assert desc.endswith("BLOCKED") and result["blocked_reason"] == "worker_capacity"
    got = store.get("alice", out["id"])
    assert got["blocker"]["code"] == "WORKER_CAPACITY" and got["blocker"]["recovery"] == "auto"
