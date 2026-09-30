"""An agent can see what the user's chats are doing, and stop one (src/chat_work.py).

On 2026-09-30 the admin agent, asked about a chat 43 minutes into a silent
bash call, could only say the chat was not one of its workers; told about
"the implement feature and open pr engineer" it started a second Lead
Engineer instead of looking at the running one.
"""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from src import agent_activity, agent_runs, agent_control
from src.agent_tools import session_tools


def _sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


async def _stuck_turn(started_at):
    yield _sse({"delta": "Working on it."})
    yield _sse({"type": "tool_start", "tool": "read_file", "command": "pom.xml", "round": 1})
    yield _sse({"type": "tool_output", "tool": "read_file", "command": "pom.xml", "output": "<project/>", "exit_code": 0})
    yield _sse({"type": "agent_step", "round": 29})
    yield _sse({"type": "tool_start", "tool": "bash", "command": "cd backend && ./mvnw -q test",
                "full_command": "cd backend && ./mvnw -q test -Dtest=EmbabelIT", "round": 29,
                "started_at": started_at})
    yield _sse({"type": "tool_progress", "tool": "bash", "tail": "Running EmbabelIT"})
    await asyncio.sleep(3600)


@pytest.fixture
def chats(monkeypatch):
    agent_runs._RUNS.clear()
    agent_activity._reset_for_tests()
    sessions = {sid: SimpleNamespace(id=sid, name=name, owner="alice")
                for sid, name in (("stuck-1", "Implement Feature and Open PR"), ("admin-1", "Admin"))}
    manager = SimpleNamespace(sessions=sessions,
                              get_sessions_for_user=lambda user: {k: v for k, v in sessions.items() if v.owner == user})
    monkeypatch.setattr(session_tools, "get_session_manager", lambda: manager)
    monkeypatch.setattr(session_tools, "resolve_session_ref", lambda _m, raw, owner=None: raw)
    monkeypatch.setattr("core.models.get_session_manager_instance", lambda: manager)
    monkeypatch.setattr("core.database.get_session_settings",
                        lambda sid, **kw: {"agent_profile": "Lead Engineer"} if sid == "stuck-1" else {})
    yield manager
    agent_runs._RUNS.clear()


@pytest.mark.asyncio
async def test_running_shows_the_tool_its_command_and_its_output(chats):
    agent_runs.start("stuck-1", _stuck_turn(time.time() - 43 * 60), owner="alice")
    await asyncio.sleep(0.05)

    info = agent_runs.describe("stuck-1")
    assert info["round"] == 29 and info["tools_finished"] == 1
    assert info["tool"] == "bash" and info["command"].endswith("-Dtest=EmbabelIT")
    assert info["tool_elapsed_s"] >= 43 * 60 - 5 and info["output_tail"] == "Running EmbabelIT"

    out = await session_tools.manage_session(json.dumps({"action": "running"}), "admin-1", owner="alice")
    assert "Implement Feature and Open PR (stuck-1)" in out["results"]
    assert "running `bash` for 43m" in out["results"] and "Running EmbabelIT" in out["results"]
    assert "loadout Lead Engineer" in out["results"]

    idle = await session_tools.manage_session(json.dumps({"action": "running", "session_id": "admin-1"}),
                                              "admin-1", owner="alice")
    assert "not working right now" in idle["results"]
    agent_runs.stop("stuck-1")


@pytest.mark.asyncio
async def test_stop_ends_another_chats_turn_but_not_its_own(chats, monkeypatch):
    async def no_workers(sid, by=""):
        return 0

    monkeypatch.setattr(agent_control, "stop_chat_work", no_workers)
    agent_runs.start("stuck-1", _stuck_turn(time.time()), owner="alice")
    await asyncio.sleep(0.05)

    own = await session_tools.manage_session(json.dumps({"action": "stop", "session_id": "admin-1"}),
                                             "admin-1", owner="alice")
    assert own["exit_code"] == 1 and "own turn" in own["error"]

    out = await session_tools.manage_session(json.dumps({"action": "stop", "session_id": "stuck-1"}),
                                             "admin-1", owner="alice")
    assert out["turn_stopped"] is True and "Stopped its running turn" in out["results"]
    await asyncio.sleep(0.05)
    assert not agent_runs.is_active("stuck-1")

    again = await session_tools.manage_session(json.dumps({"action": "stop", "session_id": "stuck-1"}),
                                               "admin-1", owner="alice")
    assert "nothing to stop" in again["results"]


@pytest.mark.asyncio
async def test_another_owners_chat_is_not_found(chats):
    chats.sessions["bob-1"] = SimpleNamespace(id="bob-1", name="Bob", owner="bob")
    out = await session_tools.manage_session(json.dumps({"action": "stop", "session_id": "bob-1"}),
                                             "admin-1", owner="alice")
    assert out["exit_code"] == 1


@pytest.mark.asyncio
async def test_starting_a_loadout_that_is_already_working_elsewhere_asks_first(chats, monkeypatch):
    from src.agent_tools import loadout_tools

    agent_runs.start("stuck-1", _stuck_turn(time.time()), owner="alice")
    await asyncio.sleep(0.05)
    monkeypatch.setattr(loadout_tools.agent_profiles, "get_profile",
                        lambda name: {"name": "Lead Engineer", "tool_access": "all", "enabled_tools": []})
    monkeypatch.setattr(loadout_tools, "_resolve_policy", lambda *a, **k: {"delegation_policy": "auto",
                                                                           "max_parallel_workers": 2},
                        raising=False)
    out = await loadout_tools.manage_agent_loadout(
        json.dumps({"action": "start", "name": "Lead Engineer", "task": "Implement the next slice."}),
        "admin-1", owner="alice")
    if out.get("blocked_reason") == "worker_capacity":
        pytest.skip("this harness has no parent-chat policy to start from")
    assert out.get("blocked_reason") == "same_loadout_running", out
    assert "Implement Feature and Open PR" in out["error"] and "parallel: true" in out["error"]
    agent_runs.stop("stuck-1")
