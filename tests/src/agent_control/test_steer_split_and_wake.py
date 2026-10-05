"""A steer splits the reply, ends long waits, and survives a turn that ends on purpose.

Reported 2026-10-02 (production): "I send a message and it gets stuck on
steering for a long time"; "the previous agent message still gets updated even
though my message is already sent"; "all my turns are grouped and all the
agent's turns are grouped in the chat".

* The reply was saved as ONE assistant message at the end, so the history read
  request, steer, steer, one long reply. It is now saved in pieces, split where
  each steer landed.
* A steer is read at the top of a round, and a round can sit inside a tool that
  waits for minutes. The waits now end when a steer arrives.
* A steer queued when the turn ends on purpose (a question for the user, the
  tool budget, a finished document) was cancelled and the text put back in the
  composer; the client now sends it as the next message.
"""

import asyncio
import json
import os
import time

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import routes.chat_routes as chat_routes
from tests.test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint
# Fixtures for the worker-status tests.
from tests.test_loadout_repo_tools import _fresh_status_backoff, runs, store  # noqa: F401

def _frame(**data):
    return f"data: {json.dumps(data)}\n\n"


@pytest.fixture
def feed(tmp_path, monkeypatch):
    from src import agent_activity, agent_control, constants

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    agent_activity._reset_for_tests()
    agent_control._STEER.clear()
    yield
    agent_control._STEER.clear()
    agent_activity._reset_for_tests()


@pytest.fixture
def clean_queue():
    """Just the steer queue: the worker-status fixtures own the activity feed."""
    from src import agent_control

    agent_control._STEER.clear()
    yield
    agent_control._STEER.clear()


# ── the saved reply is split at each steer ─────────────────────────────────

async def _run_route(monkeypatch, chunks):
    captured = {}
    order = []

    class _Log(list):
        def append(self, message):
            order.append(("user" if message.role == "user" else "assistant", message.content,
                          dict(message.metadata or {})))
            super().append(message)

    captured["added_messages"] = _Log()
    endpoint = _chat_stream_endpoint(monkeypatch, "agent", captured, agent_chunks=chunks)
    ids = iter(f"db-{n}" for n in range(1, 20))

    def fake_save(sess, session_manager, session, content, metrics, **kwargs):
        order.append(("assistant", content, dict(metrics or {})))
        return next(ids)

    monkeypatch.setattr(chat_routes, "save_assistant_response", fake_save)
    response = await endpoint(_RouteRequest("agent"))
    emitted = [chunk async for chunk in response.body_iterator]
    return order, emitted


def _events(emitted, kind):
    out = []
    for chunk in emitted:
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            data = json.loads(chunk[6:])
            if data.get("type") == kind:
                out.append(data)
    return out


@pytest.mark.asyncio
async def test_history_alternates_around_a_steer(monkeypatch, feed):
    chunks = [
        _frame(delta="First look."),
        _frame(type="tool_start", tool="bash", round=1),
        _frame(type="tool_output", tool="bash", output="ok", round=1),
        _frame(type="agent_step", round=2),
        _frame(type="steer_applied", text="also check the archive", round=2, kind="user", steer_id="steer-1"),
        _frame(delta="Second answer."),
        _frame(type="metrics", data={
            "tool_events": [{"round": 1, "tool": "bash", "output": "ok"}],
            "round_texts": ["First look.", "Second answer."],
        }),
        "data: [DONE]\n\n",
    ]
    order, emitted = await _run_route(monkeypatch, chunks)

    assert [(role, content) for role, content, _ in order] == [
        ("assistant", "First look."),
        ("user", "also check the archive"),
        ("assistant", "Second answer."),
    ]
    first, steer, last = (md for _, _, md in order)
    assert first["steer_split"] is True and first["steer_split_round"] == 2
    assert [e["tool"] for e in first["tool_events"]] == ["bash"]
    assert steer["round"] == 2 and steer["steer_id"] == "steer-1"
    # What was already saved is not saved again.
    assert "tool_events" not in last
    assert last["round_texts"] == ["", "Second answer."]
    assert "steer_split" not in last
    # Each message announces its own id, in order.
    saved = _events(emitted, "message_saved")
    assert [(e["id"], bool(e.get("steer_split"))) for e in saved] == [("db-1", True), ("db-2", False)]


@pytest.mark.asyncio
async def test_two_steers_make_three_messages_and_a_tool_only_piece_has_words(monkeypatch, feed):
    from src.agent_control import STEER_SPLIT_NO_TEXT

    chunks = [
        _frame(type="tool_start", tool="grep", round=1),
        _frame(type="tool_output", tool="grep", output="x", round=1),
        _frame(type="agent_step", round=2),
        _frame(type="steer_applied", text="one", round=2, kind="user", steer_id="s1"),
        _frame(delta="Reply to one."),
        _frame(type="agent_step", round=3),
        _frame(type="steer_applied", text="two", round=3, kind="user", steer_id="s2"),
        _frame(delta="Reply to two."),
        _frame(type="metrics", data={"round_texts": ["", "Reply to one.", "Reply to two."]}),
        "data: [DONE]\n\n",
    ]
    order, _ = await _run_route(monkeypatch, chunks)

    assert [(role, content) for role, content, _ in order] == [
        ("assistant", STEER_SPLIT_NO_TEXT),
        ("user", "one"),
        ("assistant", "Reply to one."),
        ("user", "two"),
        ("assistant", "Reply to two."),
    ]
    assert order[2][2]["round_texts"] == ["", "Reply to one."]
    assert order[4][2]["round_texts"] == ["", "", "Reply to two."]


@pytest.mark.asyncio
async def test_a_steer_before_any_output_does_not_save_an_empty_message(monkeypatch, feed):
    chunks = [
        _frame(type="steer_applied", text="wait, different file", round=1, kind="user", steer_id="s1"),
        _frame(delta="On it."),
        "data: [DONE]\n\n",
    ]
    order, _ = await _run_route(monkeypatch, chunks)
    assert [(role, content) for role, content, _ in order] == [
        ("user", "wait, different file"),
        ("assistant", "On it."),
    ]


def test_the_model_sees_the_whole_turn_in_the_order_it_happened(feed):
    """get_context_messages is a plain walk of the history, so the split pieces
    reach the next turn as: request, first part, instruction, second part."""
    from core.models import ChatMessage, Session
    from src.agent_control import persist_applied_steer, persist_steer_split

    sess = Session(id="ctx", name="n", endpoint_url="http://x/v1", model="m")
    sess.add_message(ChatMessage("user", "audit the repo"))
    persist_steer_split(sess, content="I read the config.", metadata={"tool_events": [{"round": 1}]})
    persist_applied_steer(sess, {"text": "skip tests", "steer_id": "s1", "round": 2, "kind": "user"})
    sess.add_message(ChatMessage("assistant", "Audit done, tests skipped."))
    sess.add_message(ChatMessage("user", "thanks"))

    roles = [(m["role"], m["content"]) for m in sess.get_context_messages()]
    # The piece that ran tools also carries the record of them (src/turn_trail.py).
    trail = sess.history[1].metadata["model_trail"]
    assert roles == [
        ("user", "audit the repo"),
        ("assistant", "I read the config." + chr(10) * 2 + trail),
        ("user", "skip tests"),
        ("assistant", "Audit done, tests skipped."),
        ("user", "thanks"),
    ]
    assert sess.history[1].metadata["steer_split"] is True


def test_headless_loop_splits_at_a_steer_too(feed, monkeypatch):
    import src.agent_loop as agent_loop
    import src.model_context as model_context
    from core.models import Session
    from src.headless_agent import run_headless

    async def fake_loop(url, model, messages, **kwargs):
        yield _frame(delta="Surveyed the module.")
        yield _frame(type="tool_start", tool="read_file", command="a.py")
        yield _frame(type="tool_output", tool="read_file", output="ok", exit_code=0)
        yield _frame(type="agent_step", round=2)
        yield _frame(type="steer_applied", text="focus on db.py", round=2, kind="user", steer_id="s1")
        yield _frame(delta="Focused on db.py.")
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop, raising=False)
    monkeypatch.setattr(model_context, "get_context_length", lambda url, model: 32_000)
    sess = Session(id="w-split", name="worker", endpoint_url="http://llm.test/v1", model="m")

    text, events = asyncio.run(run_headless(sess, [{"role": "user", "content": "go"}]))

    # The caller saves only what came after the steer.
    assert text == "Focused on db.py."
    assert events == []
    assert [(m.role, m.content) for m in sess.history] == [
        ("assistant", "Surveyed the module."),
        ("user", "focus on db.py"),
    ]
    assert sess.history[0].metadata["tool_events"][0]["tool"] == "read_file"


# ── long waits end when a steer arrives ────────────────────────────────────

async def test_status_wait_returns_early_with_a_note_when_a_steer_arrives(store, runs, clean_queue):  # noqa: F811
    from src import agent_control
    from src.agent_tools.loadout_tools import manage_agent_loadout

    async def steer_soon():
        await asyncio.sleep(0.3)
        agent_control.steer("chat-7", "stop and look at the logs")

    task = asyncio.ensure_future(steer_soon())
    started = time.monotonic()
    result = await manage_agent_loadout(
        '{"action": "status", "run_id": "session-6caedad9d9", "wait_seconds": 60}', "chat-7", owner="u")
    await task

    assert time.monotonic() - started < 5
    # The normal "still running" answer, plus why it came back early.
    assert result["running"] == 1
    assert "still running" in result["response"]
    assert result["steer_note"] == agent_control.STEER_WAIT_NOTE
    assert "nothing was cancelled" in result["response"]


async def test_a_steer_already_queued_ends_the_wait_at_once(store, runs, clean_queue):  # noqa: F811
    from src import agent_control
    from src.agent_tools.loadout_tools import manage_agent_loadout

    agent_control.steer("chat-7", "already here")
    started = time.monotonic()
    result = await manage_agent_loadout(
        '{"action": "status", "run_id": "session-6caedad9d9", "wait_seconds": 60}', "chat-7", owner="u")
    assert time.monotonic() - started < 3
    assert result["running"] == 1 and "steer_note" in result


async def test_wait_or_steer_never_cancels_what_it_waits_on(feed):
    from src import agent_control

    work = asyncio.ensure_future(asyncio.sleep(30))
    waiter = asyncio.ensure_future(agent_control.wait_or_steer("s", 30, work))
    await asyncio.sleep(0.05)
    agent_control.steer("s", "hey")
    assert await asyncio.wait_for(waiter, 3) is True
    assert not work.done() and not work.cancelled()
    work.cancel()


async def test_wait_or_steer_reports_a_finished_job_as_not_a_steer(feed):
    from src import agent_control

    work = asyncio.ensure_future(asyncio.sleep(0.05))
    assert await agent_control.wait_or_steer("s", 10, work) is False
    assert work.done()


async def test_wait_or_steer_times_out_without_a_steer(feed):
    from src import agent_control

    started = time.monotonic()
    assert await agent_control.wait_or_steer("s", 0.1) is False
    assert time.monotonic() - started < 2
    assert agent_control._WAKERS == {}, "the waiter must unregister"


async def test_workflow_wait_returns_early_on_a_steer(feed, monkeypatch):
    from src import agent_control, agent_workflows

    never = asyncio.ensure_future(asyncio.sleep(30))
    monkeypatch.setattr(agent_workflows, "_load", lambda *a, **k: {"id": "wf"})
    monkeypatch.setattr(agent_workflows, "_public", lambda rec: {"terminal": False, "workflow_id": "wf"})
    monkeypatch.setitem(agent_workflows._TASKS, "wf", never)
    agent_control.steer("chat-9", "change of plan")
    started = time.monotonic()
    result = await agent_workflows.inspect(workflow_id="wf", session_id="chat-9", owner="u",
                                           action="wait", wait_seconds=60)
    assert time.monotonic() - started < 3
    assert result["steer_note"] == agent_control.STEER_WAIT_NOTE
    assert not never.done()
    never.cancel()


def test_only_calls_that_just_wait_are_skipped_when_a_steer_is_pending():
    from src.agent_control import is_wait_only_call as wait_only

    assert wait_only("manage_agent_loadout", '{"action": "status", "wait_seconds": 120}')
    assert wait_only("manage_agent_loadout", '{"action": "wait", "run_id": "r"}')
    assert wait_only("delegate_to_claude_code", '{"action": "poll", "task_id": "t", "wait_seconds": 60}')
    assert wait_only("orchestrate_agents", '{"action": "wait"}')
    # Looking without waiting, and anything that changes state, still runs.
    assert not wait_only("manage_agent_loadout", '{"action": "status"}')
    assert not wait_only("manage_agent_loadout", '{"action": "start", "wait_seconds": 5}')
    assert not wait_only("delegate_to_claude_code", '{"action": "run", "prompt": "x"}')
    assert not wait_only("bash", '{"action": "poll", "wait_seconds": 5}')
    assert not wait_only("manage_agent_loadout", "not json")
