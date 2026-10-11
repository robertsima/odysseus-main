"""A capacity refusal says which worker holds the slot and what to do about it.

Bug (2026-10-10): a resume of the Lead Engineer worker was refused with "1 active
of this chat's limit 1" while the one active worker WAS the Lead Engineer. The
message named neither the worker nor any way out, so the person had to stop the
run by hand to send one more line.
"""
import asyncio
import json

import pytest

import src.tool_execution as te
from src import agent_activity, agent_control, agent_mailbox
from src.agent_tools import ToolBlock

CHAT = "lead-chat"
WORKER = "worker-lead-engineer"


@pytest.fixture
def live_worker(monkeypatch, tmp_path):
    """CHAT has one running child worker, and a limit of one."""
    from src import constants

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    agent_activity._reset_for_tests()
    agent_control._STEER.clear()
    agent_mailbox._SEND_COUNTS.clear()
    agent_activity.run_started(
        WORKER, "session", "Lead Engineer: build export", run_id="run-1", data={"parent_session": CHAT})
    monkeypatch.setattr("core.database.get_session_settings", lambda sid, **kw: {})
    monkeypatch.setattr("src.settings.load_disabled_tools_strict", lambda: [])
    monkeypatch.setattr("src.tool_security.owner_baseline_disabled_tools", lambda owner: set())
    monkeypatch.setattr(agent_mailbox, "_session_owner", lambda sid: "alice")
    yield
    agent_activity._reset_for_tests()
    agent_control._STEER.clear()
    agent_mailbox._SEND_COUNTS.clear()


def _call(tool, content):
    return asyncio.run(te.execute_tool_block(
        ToolBlock(tool, content), session_id=CHAT, owner="alice", security_context=te.NO_TOOL_SECURITY_CONTEXT))[1]


def test_the_refusal_names_the_worker_and_the_options(live_worker):
    result = _call("send_to_session", json.dumps({"session_id": "new", "message": "another task", "mode": "agent"}))

    assert result["blocked_reason"] == "worker_capacity"
    assert WORKER in result["error"] and "Lead Engineer" in result["error"]
    for option in ("hand-back", "send_to_session", "stop"):
        assert option in result["error"]
    holder, = result["capacity"]["holders"]
    assert holder["session_id"] == WORKER and holder["title"] == "Lead Engineer: build export"


def test_a_message_to_the_worker_holding_the_slot_is_steered_not_refused(live_worker):
    result = _call("send_to_session", json.dumps({"session_id": WORKER, "message": "please just finish this",
                                                  "mode": "agent"}))

    assert result.get("blocked_reason") != "worker_capacity", result
    assert result["steered"] is True and result["to_session"] == WORKER
    queued = agent_control.pending_steer(WORKER)
    assert len(queued) == 1 and "please just finish this" in queued[0]["text"]


def test_a_message_to_a_different_session_is_still_refused(live_worker):
    result = _call("send_to_session", json.dumps({"session_id": "someone-else", "message": "hi", "mode": "agent"}))

    assert result["blocked_reason"] == "worker_capacity"
    assert not agent_control.pending_steer("someone-else")


def test_when_the_steer_is_refused_the_capacity_message_says_why(live_worker, monkeypatch):
    monkeypatch.setattr(agent_mailbox, "get_setting",
                        lambda key, default=None: False if key == "agent_peer_messaging" else default)

    result = _call("send_to_session", json.dumps({"session_id": WORKER, "message": "finish", "mode": "agent"}))

    assert result["blocked_reason"] == "worker_capacity"
    assert "messaging is turned off" in result["error"]
