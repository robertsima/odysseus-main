"""A finished worker continues the person's request, and only that far.

When a worker finished, ``agent_control._hand_off`` once continued the parent
chat with every launcher its chat had. Nothing the user typed was behind that
turn, yet it could start new workers -- and each of those, on finishing,
continued the chat again: a chat that kept spawning workers after the person
had stopped talking to it. The fix then went all the way the other way (no
launcher ever), which made every blocked worker a dead end: on 2026-09-29 the
user typed "retry" about 17 times for 3 delivered fixes.

The rule pinned here: a follow-up may send work out again (the same worker via
send_to_session, or another) only while the request that started the chain has
follow-ups left (``agent_auto_continue_limit``, counted per launch, reset by
the person's next message). A stopped worker, a spent budget or a limit of 0
means it only reports. Sibling workers finishing together produce one reply.
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


def _limit(monkeypatch, n):
    monkeypatch.setattr(agent_control, "_auto_continue_limit", lambda: n)


def _launch_event(tool="send_to_session", command='{"session_id": "w-1", "mode": "agent"}', exit_code=0):
    return {"tool": tool, "command": command, "output": "ok", "exit_code": exit_code}


async def test_with_follow_ups_disabled_a_completion_can_never_launch_a_worker(followups, monkeypatch):
    _limit(monkeypatch, 0)
    parent = _Chat("parent")
    parent.add_message(_Msg("user", "scout the repo"))
    await agent_control._hand_off(_Manager(parent), "parent", _Worker(), "scout it",
                                  "Found issue #50.", "completed", "alice")

    assert len(followups) == 1, "one summarising reply, no more"
    assert headless_agent.SUBAGENT_BLOCKED_TOOLS <= followups[0]["disabled_tools"]
    # The result is recorded, then summarised once.
    assert [m.metadata.get("source") for m in parent.history] == [None, "worker", "worker_followup"]
    # And the model is told to report, with the worker's session to continue from.
    assert "the user decides whether to continue" in parent.history[1].content


async def test_a_follow_up_may_send_the_same_worker_back_while_the_request_has_budget(followups, monkeypatch):
    _limit(monkeypatch, 3)
    parent = _Chat("parent")
    parent.add_message(_Msg("user", "fix the failing Jest suite and open a PR"))
    await agent_control._hand_off(_Manager(parent), "parent", _Worker(), "fix it",
                                  "Blocked: jest-expo is missing.\nNeeds parent: a workspace with dependencies",
                                  "completed", "alice")

    denied = followups[0]["disabled_tools"]
    assert not agent_control._CONTINUE_LAUNCH_TOOLS & denied
    # Only the launchers that carry the request on; nothing else that makes chats.
    assert {"create_session", "pipeline", "manage_session"} <= denied
    inject = parent.history[1].content
    assert "fix the failing Jest suite and open a PR" in inject      # the goal, quoted
    assert '"w-1"' in inject and "send_to_session" in inject          # resume, not respawn
    assert "Automatic follow-ups left for this request: 3" in inject
    assert "this chat can unblock it: a workspace with dependencies" in inject


async def test_each_launch_spends_one_follow_up_until_none_are_left(monkeypatch):
    _limit(monkeypatch, 2)
    calls = []

    async def fake_headless(sess, messages, **kwargs):
        calls.append(set(kwargs.get("disabled_tools") or ()))
        return "Sent the worker back with the fix.", [_launch_event()]

    monkeypatch.setattr(headless_agent, "run_headless", fake_headless)
    parent = _Chat("parent")
    manager = _Manager(parent)
    parent.add_message(_Msg("user", "fix the build"))
    for _ in range(3):
        await agent_control._hand_off(manager, "parent", _Worker(), "fix it", "Blocked: tests fail.",
                                      "completed", "alice")

    followups = [m for m in parent.history if m.metadata.get("source") == "worker_followup"]
    assert [m.metadata.get("continued") for m in followups] == [1, 1, None]
    assert not agent_control._CONTINUE_LAUNCH_TOOLS & calls[0]
    assert not agent_control._CONTINUE_LAUNCH_TOOLS & calls[1]
    # The third hand-back finds the request's follow-ups spent: report only.
    assert headless_agent.SUBAGENT_BLOCKED_TOOLS <= calls[2]
    assert "This request has used its automatic follow-ups" in parent.history[-2].content


async def test_the_persons_next_message_starts_a_fresh_count(monkeypatch):
    _limit(monkeypatch, 1)
    parent = _Chat("parent")
    parent.add_message(_Msg("user", "fix the build"))
    parent.add_message(_Msg("assistant", "Sent it back.", {"source": "worker_followup", "continued": 1}))
    assert agent_control._continuation_budget(parent) == 0
    # A hand-back or a steer is not the person's request ...
    parent.add_message(_Msg("user", "[Worker W finished] ...", {"source": "worker"}))
    parent.add_message(_Msg("user", "also check lint", {"source": "steer"}))
    assert agent_control._continuation_budget(parent) == 0
    # ... their own message is.
    parent.add_message(_Msg("user", "ok, now also update the docs"))
    assert agent_control._continuation_budget(parent) == 1


async def test_a_stopped_worker_never_continues(followups, monkeypatch):
    _limit(monkeypatch, 3)
    parent = _Chat("parent")
    parent.add_message(_Msg("user", "fix the build"))
    await agent_control._hand_off(_Manager(parent), "parent", _Worker(), "fix it",
                                  "(stopped from the chat's Stop button before finishing)", "cancelled", "alice")
    assert headless_agent.SUBAGENT_BLOCKED_TOOLS <= followups[0]["disabled_tools"]
    assert "The user stopped this worker" in parent.history[1].content


async def test_a_failed_launch_spends_nothing(monkeypatch):
    _limit(monkeypatch, 1)

    async def fake_headless(sess, messages, **kwargs):
        return "Tried to start one.", [_launch_event("manage_agent_loadout", '{"action": "start"}', exit_code=1)]

    monkeypatch.setattr(headless_agent, "run_headless", fake_headless)
    parent = _Chat("parent")
    parent.add_message(_Msg("user", "fix the build"))
    await agent_control._hand_off(_Manager(parent), "parent", _Worker(), "fix it", "Blocked.", "completed", "alice")
    assert parent.history[-1].metadata.get("continued") is None
    assert agent_control._continuation_budget(parent) == 1


def test_launches_are_counted_per_call():
    events = [_launch_event(),
              _launch_event("manage_agent_loadout", '{"action":"start","name":"Lead Engineer"}'),
              _launch_event("manage_agent_loadout", '{"action": "status"}'),
              {"tool": "read_file", "command": "x", "exit_code": 0}]
    assert agent_control._launches(events) == 2


async def test_the_launcher_denial_reaches_the_agent_loop(monkeypatch):
    """The same property through the real ``run_headless``: the loop that
    would execute a launcher is handed it as disabled once follow-ups are off."""
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
                        lambda key, default=None: [] if key == "disabled_tools"
                        else 0 if key == "agent_auto_continue_limit" else default)
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


def test_a_reply_from_the_turn_that_was_running_does_not_count_as_covering():
    """2026-09-28 08:06: both results arrived mid-turn; that turn's reply (about
    something else) was saved after them. It never saw them."""
    parent = _Chat("parent")
    parent.add_message(_Msg("user", "fix the lead engineer"))
    parent.add_message(_Msg("user", "[Worker A finished]", {"source": "worker", "from_session": "w-a",
                                                           "arrived_mid_turn": True}))
    parent.add_message(_Msg("assistant", "Lead Engineer preset updated."))
    assert agent_control._replied_after_worker_result(parent) is False
    # A later answer (a turn that started after the result) does count.
    parent.add_message(_Msg("assistant", "Worker A found X."))
    assert agent_control._replied_after_worker_result(parent) is True


def test_status_with_a_stored_result_counts_as_consulting_the_worker():
    parent = _Chat("parent")
    parent.add_message(_Msg("user", "research"))
    parent.add_message(_Msg("user", "[Worker A finished]", {"source": "worker", "from_session": "w-a"}))
    parent.add_message(_Msg("assistant", "A found X.", {"tool_events": [
        {"tool": "manage_agent_loadout", "command": '{"action": "status", "worker_session": "session-a5"}',
         "output": '{"runs": [{"worker_session": "w-a", "result_ref": "toolout-1", "result_chars": 9000}]}'},
    ]}))
    assert agent_control._results_consulted_by_later_turn(parent) is True


async def test_judgement_uses_a_small_context(monkeypatch):
    seen = {}

    async def fake_headless(sess, messages, **kwargs):
        seen["messages"] = messages
        return agent_control._NO_UPDATE_MARKER, []

    monkeypatch.setattr(headless_agent, "run_headless", fake_headless)
    parent = _Chat("parent")
    for i in range(50):
        parent.add_message(_Msg("user", f"old question {i}"))
        parent.add_message(_Msg("assistant", f"old answer {i}"))
    parent.add_message(_Msg("user", "research X"))
    parent.add_message(_Msg("user", "[Worker A finished]\nResult: X", {"source": "worker", "from_session": "w-a"}))
    parent.add_message(_Msg("assistant", "Reported X."))
    await agent_control._continue_parent(_Manager(parent), "parent", parent, _Worker("w-a", "A"), "alice")
    contents = [m["content"] for m in seen["messages"]]
    assert contents == ["research X", "[Worker A finished]\nResult: X", "Reported X.",
                        agent_control._ALREADY_ANSWERED_NOTE]
    assert "reply" not in agent_control._ALREADY_ANSWERED_NOTE.lower()


async def test_results_waiting_on_a_busy_chat_share_one_follow_up(monkeypatch, followups):
    import asyncio
    busy = {"v": True}
    monkeypatch.setattr(agent_runs, "is_busy", lambda sid: busy["v"])
    parent = _Chat("parent")
    manager = _Manager(parent)
    for wid in ("w-1", "w-2", "w-3"):
        await agent_control._hand_off(manager, "parent", _Worker(wid, wid), "part", f"result {wid}",
                                      "completed", "alice")
    assert len(agent_control._WAITING_PARENTS) == 1
    busy["v"] = False
    for _ in range(50):
        if not agent_control._PENDING_HANDOFFS:
            break
        await asyncio.sleep(0.05)
    assert len(followups) == 1
