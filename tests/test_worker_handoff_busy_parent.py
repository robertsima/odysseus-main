"""A worker that finishes while its parent chat is mid-turn.

That turn built its context before the result existed, so it never read it,
and nothing continued afterwards: the result sat in history until the user
happened to send something, and the worker looked like it had produced
nothing. The parent now continues once its turn ends.
"""
import asyncio

import pytest

from src import agent_control, agent_runs


class _Msg:
    def __init__(self, role, content, metadata=None):
        self.role, self.content, self.metadata = role, content, metadata or {}


class _Chat:
    def __init__(self, sid):
        self.id, self.name, self.model = sid, sid, "m"
        self.history = []

    def add_message(self, message):
        self.history.append(message)

    def get_context_messages(self):
        return [{"role": m.role, "content": m.content} for m in self.history]


class _Manager:
    def __init__(self, chat):
        self.chat = chat

    def get_session(self, sid):
        return self.chat if sid == self.chat.id else None

    def save_sessions(self):
        pass


class _Worker:
    id, name = "w-1", "Scout"


@pytest.fixture
def busy(monkeypatch):
    state = {"busy": True}
    monkeypatch.setattr(agent_runs, "is_busy", lambda sid: state["busy"])
    monkeypatch.setattr(agent_control, "_HANDOFF_IDLE_POLL_S", 0.01)
    ran = []

    async def fake_headless(sess, messages, **kwargs):
        ran.append(messages)
        return "Here is the scout's result, summarised.", []

    import src.headless_agent as headless
    monkeypatch.setattr(headless, "run_headless", fake_headless)
    return state, ran


async def test_parent_continues_once_its_turn_ends(busy):
    state, ran = busy
    parent = _Chat("parent")
    await agent_control._hand_off(_Manager(parent), "parent", _Worker(), "scout it", "Found issue #50.",
                                  "completed", "alice")
    assert parent.history[-1].metadata["source"] == "worker"
    await asyncio.sleep(0.05)
    assert not ran, "must not run while the parent's turn is still going"

    # The turn that was running ends with its own reply, then goes idle.
    parent.add_message(_Msg("assistant", "Worker launched."))
    state["busy"] = False
    for _ in range(50):
        if ran:
            break
        await asyncio.sleep(0.01)
    await asyncio.gather(*list(agent_control._PENDING_HANDOFFS))

    assert ran and any("Found issue #50." in m["content"] for m in ran[0])
    assert parent.history[-1].metadata["source"] == "worker_followup"


async def test_no_second_continuation_when_a_later_turn_already_read_it(busy):
    state, ran = busy
    parent = _Chat("parent")
    await agent_control._hand_off(_Manager(parent), "parent", _Worker(), "scout it", "Found issue #50.",
                                  "completed", "alice")
    # The user sent a message after the result arrived; that turn had it.
    parent.add_message(_Msg("user", "what did it find?"))
    state["busy"] = False
    await asyncio.gather(*list(agent_control._PENDING_HANDOFFS))
    assert not ran


async def test_gives_up_after_the_wait_limit(busy, monkeypatch):
    state, ran = busy
    monkeypatch.setattr(agent_control, "_HANDOFF_IDLE_WAIT_S", 0.03)
    parent = _Chat("parent")
    await agent_control._hand_off(_Manager(parent), "parent", _Worker(), "scout it", "Found issue #50.",
                                  "completed", "alice")
    await asyncio.gather(*list(agent_control._PENDING_HANDOFFS))
    assert not ran
    assert parent.history[-1].metadata["source"] == "worker"  # still saved for later


async def test_a_running_worker_chat_shows_its_task(monkeypatch):
    """The task used to be saved only when the run ended, so opening a running
    worker's chat showed an empty "New chat ready" screen."""
    import src.agent_tools.session_tools as session_tools
    import src.ai_interaction as ai_interaction
    import src.headless_agent as headless

    worker_chat = _Chat("w-2")
    seen = {}
    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: _Manager(worker_chat))
    monkeypatch.setattr(session_tools, "_new_child_session", lambda *a, **k: (worker_chat, None))

    async def fake_headless(sess, messages, **kwargs):
        seen["history_during_run"] = [m.content for m in sess.history]
        return "A poem.", []

    monkeypatch.setattr(headless, "run_headless", fake_headless)
    rec = await agent_control.launch_worker(owner="alice", task="write a poem", preflight=False)
    await agent_control._WORKERS[rec["run_id"]]
    assert seen["history_during_run"] == ["write a poem"]
    assert [m.content for m in worker_chat.history] == ["write a poem", "A poem."]  # not saved twice
