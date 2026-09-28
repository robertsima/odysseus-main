"""A finished worker must not set the chat running on its own.

When a worker finished, ``agent_control._hand_off`` continued the parent chat
with a headless turn that had every launcher its chat had. Nothing the user
typed was behind that turn, yet it could start new workers — and each of
those, on finishing, continued the chat again. The user saw a chat that kept
spawning workers after they had stopped talking to it.

The rule pinned here: a completion is recorded in the parent chat and may
produce at most one summarising reply; that reply can never launch a worker,
and sibling workers finishing together produce one reply, not one each.
"""
import json

import pytest

from src import agent_activity as act
from src import agent_control, agent_runs, constants
from src import headless_agent


class _Msg:
    def __init__(self, role, content, metadata=None):
        self.role, self.content, self.metadata = role, content, metadata or {}


class _Chat:
    def __init__(self, sid):
        self.id, self.name, self.model = sid, sid, "m"
        # Read by a real `run_headless`: the endpoint to call and a window, so
        # it does not go to the network probing for one.
        self.endpoint_url, self.context_length, self.headers = "http://llm.test/v1", 200_000, {}
        self.history = []

    def add_message(self, message):
        self.history.append(message)

    def get_context_messages(self):
        return [{"role": m.role, "content": m.content} for m in self.history]


class _Manager:
    def __init__(self, *chats):
        self.chats = {c.id: c for c in chats}

    def get_session(self, sid):
        return self.chats.get(sid)

    def save_sessions(self):
        pass


class _Worker:
    def __init__(self, sid="w-1", name="Scout"):
        self.id, self.name = sid, name


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    act._reset_for_tests()
    monkeypatch.setattr(agent_runs, "is_busy", lambda sid: False)
    yield
    act._reset_for_tests()


@pytest.fixture
def followups(monkeypatch):
    """Record every follow-up turn and the tools it was denied."""
    ran = []

    async def fake_headless(sess, messages, **kwargs):
        ran.append({"session": sess.id, "disabled_tools": set(kwargs.get("disabled_tools") or ())})
        return "Summary of what the workers found.", []

    monkeypatch.setattr(headless_agent, "run_headless", fake_headless)
    return ran


async def test_a_completion_follow_up_can_never_launch_a_worker(followups):
    parent = _Chat("parent")
    await agent_control._hand_off(_Manager(parent), "parent", _Worker(), "scout it",
                                  "Found issue #50.", "completed", "alice")

    assert len(followups) == 1, "one summarising reply, no more"
    assert headless_agent.SUBAGENT_BLOCKED_TOOLS <= followups[0]["disabled_tools"]
    # The result is recorded, then summarised once.
    assert [m.metadata.get("source") for m in parent.history] == ["worker", "worker_followup"]
    # And the model is told not to go looking for more work either.
    assert "do not start new workers" in parent.history[0].content


async def test_the_launcher_denial_reaches_the_agent_loop(monkeypatch):
    """The same property through the real ``run_headless``: the loop that
    would execute a launcher is handed it as disabled."""
    import core.database as core_db
    import src.agent_loop as agent_loop
    import src.settings as settings
    import src.tool_security as tool_security

    seen = {}

    async def fake_loop(url, model, messages, **kwargs):
        seen.update(kwargs)
        yield f"data: {json.dumps({'delta': 'summarised'})}\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)
    monkeypatch.setattr(settings, "get_setting",
                        lambda key, default=None: [] if key == "disabled_tools" else default)
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: True)
    monkeypatch.setattr(core_db, "get_session_settings", lambda sid: {})

    parent = _Chat("parent")
    await agent_control._hand_off(_Manager(parent), "parent", _Worker(), "scout it",
                                  "Found issue #50.", "completed", "alice")

    for tool in ("orchestrate_agents", "manage_agent_loadout", "delegate_to_agent", "create_session"):
        assert tool in set(seen["disabled_tools"]), tool
    assert parent.history[-1].content == "summarised"


async def test_siblings_finishing_produce_one_summary_not_one_each(followups):
    parent = _Chat("parent")
    manager = _Manager(parent)
    runs = [act.run_started(f"w-{i}", "session", f"Worker {i}", owner="alice",
                            data={"parent_session": "parent"}) for i in (1, 2, 3)]

    # The first two finish while a sibling is still running: recorded, no turn.
    for i in (1, 2):
        act.run_finished(f"w-{i}", "session", runs[i - 1], f"Worker {i} completed", owner="alice")
        await agent_control._hand_off(manager, "parent", _Worker(f"w-{i}", f"W{i}"), "part",
                                      f"result {i}", "completed", "alice")
    assert followups == []
    assert [m.content.startswith("[Worker W") for m in parent.history] == [True, True]

    # The last one runs the single summary, which has every result in context.
    act.run_finished("w-3", "session", runs[2], "Worker 3 completed", owner="alice")
    await agent_control._hand_off(manager, "parent", _Worker("w-3", "W3"), "part",
                                  "result 3", "completed", "alice")
    assert len(followups) == 1
    assert [m.metadata.get("source") for m in parent.history] == ["worker"] * 3 + ["worker_followup"]


def _answered_parent():
    """The 2026-09-28 shape: the chat's own turn was running when the worker
    finished, read the result itself, and replied after it arrived."""
    parent = _Chat("parent")
    parent.add_message(_Msg("user", "[Worker Scout finished]\nResult:\nFound issue #50.", {"source": "worker"}))
    parent.add_message(_Msg("assistant", "Updated the note with issue #50 and reported it."))
    return parent


async def test_a_result_the_chat_already_reported_posts_no_second_summary(monkeypatch):
    seen = {}

    async def fake_headless(sess, messages, **kwargs):
        seen["messages"] = messages
        return agent_control._NO_UPDATE_MARKER, []

    monkeypatch.setattr(headless_agent, "run_headless", fake_headless)
    parent = _answered_parent()
    await agent_control._continue_parent(_Manager(parent), "parent", parent, _Worker(), "alice")

    # The follow-up was asked to judge coverage (a one-off note, not saved) ...
    assert seen["messages"][-1]["content"] == agent_control._ALREADY_ANSWERED_NOTE
    # ... and a covered result adds nothing to the chat.
    assert [m.role for m in parent.history] == ["user", "assistant"]
    assert all(agent_control._NO_UPDATE_MARKER not in m.content for m in parent.history)


async def test_an_uncovered_result_still_gets_its_follow_up(monkeypatch):
    async def fake_headless(sess, messages, **kwargs):
        return "One thing the reply above missed: issue #51.", []

    monkeypatch.setattr(headless_agent, "run_headless", fake_headless)
    parent = _answered_parent()
    await agent_control._continue_parent(_Manager(parent), "parent", parent, _Worker(), "alice")
    assert parent.history[-1].metadata.get("source") == "worker_followup"
    assert "issue #51" in parent.history[-1].content


async def test_no_coverage_note_when_the_chat_has_not_replied_since(followups, monkeypatch):
    seen = {}

    async def fake_headless(sess, messages, **kwargs):
        seen["messages"] = messages
        return "Summary.", []

    monkeypatch.setattr(headless_agent, "run_headless", fake_headless)
    parent = _Chat("parent")
    await agent_control._hand_off(_Manager(parent), "parent", _Worker(), "scout it",
                                  "Found issue #50.", "completed", "alice")
    assert all(m["content"] != agent_control._ALREADY_ANSWERED_NOTE for m in seen["messages"])
    assert parent.history[-1].content == "Summary."


async def test_results_the_turn_fetched_from_the_workers_skip_the_follow_up(monkeypatch):
    """Deterministic path: the chat's turn messaged each worker and got its
    full answer back, so no follow-up turn (and no model call) runs."""
    calls = []

    async def fake_headless(sess, messages, **kwargs):
        calls.append(1)
        return "duplicate", []

    monkeypatch.setattr(headless_agent, "run_headless", fake_headless)
    parent = _Chat("parent")
    parent.add_message(_Msg("user", "research X"))
    for wid in ("w-a", "w-b"):
        parent.add_message(_Msg("user", f"[Worker {wid} finished]\nResult: ...",
                                {"source": "worker", "from_session": wid}))
    parent.add_message(_Msg("assistant", "Here is what both workers found.", {"tool_events": [
        {"tool": "send_to_session", "command": '{"session_id": "w-a", "message": "full result?"}',
         "output": "A" * 800},
        {"tool": "send_to_session", "command": '{"session_id": "w-b", "message": "full result?"}',
         "output": "B" * 800},
    ]}))
    await agent_control._continue_parent(_Manager(parent), "parent", parent, _Worker("w-b", "B"), "alice")
    assert calls == []
    assert parent.history[-1].content == "Here is what both workers found."


async def test_one_unconsulted_result_still_gets_the_follow_up(monkeypatch):
    calls = []

    async def fake_headless(sess, messages, **kwargs):
        calls.append(1)
        return "B found something the reply missed.", []

    monkeypatch.setattr(headless_agent, "run_headless", fake_headless)
    parent = _Chat("parent")
    parent.add_message(_Msg("user", "research X"))
    for wid in ("w-a", "w-b"):
        parent.add_message(_Msg("user", f"[Worker {wid} finished]", {"source": "worker", "from_session": wid}))
    parent.add_message(_Msg("assistant", "A's findings.", {"tool_events": [
        {"tool": "send_to_session", "command": '{"session_id": "w-a"}', "output": "A" * 800},
        # A status poll on B proves nothing about its content.
        {"tool": "manage_agent_loadout", "command": '{"action": "status", "run_id": "w-b"}', "output": "B" * 800},
    ]}))
    await agent_control._continue_parent(_Manager(parent), "parent", parent, _Worker("w-b", "B"), "alice")
    assert calls == [1]
    assert "missed" in parent.history[-1].content
