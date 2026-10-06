import asyncio
import json
import sys
import types
from types import SimpleNamespace

from src import bg_monitor


def test_drain_agent_ignores_non_string_deltas(monkeypatch):
    async def fake_stream_agent_loop(*args, **kwargs):
        yield 'data: {"delta": null}'
        yield 'data: {"delta": ["bad"]}'
        yield 'data: {"delta": "ok"}'
        yield 'data: {"type": "agent_step", "round": 2}'
        yield 'data: {"type": "tool_output", "tool": "shell", "output": "done"}'
        yield "data: [DONE]"

    agent_loop = types.ModuleType("src.agent_loop")
    agent_loop.stream_agent_loop = fake_stream_agent_loop
    monkeypatch.setitem(sys.modules, "src.agent_loop", agent_loop)

    sess = SimpleNamespace(
        endpoint_url="http://example.test",
        model="model",
        headers=None,
        context_length=0,
        id="s1",
    )

    full, events = asyncio.run(bg_monitor._drain_agent(sess, []))

    assert full == "ok"
    assert events == [{
        "round": 2,
        "tool": "shell",
        "command": None,
        "output": "done",
        "exit_code": None,
    }]


def test_background_job_output_is_wrapped_and_arms_gate(monkeypatch):
    monkeypatch.setattr(bg_monitor.bg_jobs, "result_text", lambda rec: "injected output")

    message = bg_monitor._background_result_message({"id": "job-1"})

    assert message["metadata"]["trusted"] is False
    assert message["metadata"]["tool_gate_untrusted"] is True
    assert "injected output" in message["content"]


def test_background_drain_preserves_exact_approval_card(monkeypatch):
    approval = {
        "kind": "tool_approval",
        "approval_id": "opaque-id",
        "question": "Allow this exact action once?",
        "options": [{"label": "Allow once"}, {"label": "Deny"}],
    }

    async def fake_stream_agent_loop(*args, **kwargs):
        yield "data: " + json.dumps({
            "type": "tool_output",
            "tool": "bash",
            "command": "echo ok",
            "output": "Waiting for an exact user approval.",
            "exit_code": None,
            "ask_user": approval,
        })
        yield "data: [DONE]"

    agent_loop = types.ModuleType("src.agent_loop")
    agent_loop.stream_agent_loop = fake_stream_agent_loop
    monkeypatch.setitem(sys.modules, "src.agent_loop", agent_loop)

    sess = SimpleNamespace(
        endpoint_url="http://example.test",
        model="model",
        headers=None,
        context_length=0,
        id="s1",
        owner="owner",
    )

    _, events = asyncio.run(bg_monitor._drain_agent(sess, []))

    assert events[0]["ask_user"] == approval


class _FakeManager:
    def __init__(self, sess):
        self.sess = sess
        self.added = []

    def get_session(self, sid):
        return self.sess if sid == self.sess.id else None

    def add_message(self, sid, msg):
        self.added.append((sid, msg))

    def save_sessions(self):
        pass


def _followup_fixture(monkeypatch, drained):
    from src import agent_activity, ai_interaction

    sess = SimpleNamespace(id="worker-1", owner="admin", model="m", get_context_messages=lambda: [])
    manager = _FakeManager(sess)
    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: manager)
    monkeypatch.setattr(agent_activity, "publish", lambda *a, **k: None)
    monkeypatch.setattr(agent_activity, "run_finished", lambda *a, **k: None)
    monkeypatch.setattr(bg_monitor.bg_jobs, "result_text", lambda rec: "exit 0")

    async def fake_drain(sess, messages, *, run_id=None):
        drained.append(run_id)
        return "worker finished the task", []

    monkeypatch.setattr(bg_monitor, "_drain_agent", fake_drain)
    return sess, manager


def test_followup_waits_for_a_running_worker_turn(monkeypatch):
    # 2026-10-06: a worker started `npm ci` in the background; the job ended
    # while the worker's own headless turn was still running, and the follow-up
    # started a second loop on the same chat. Each loop then saw the other's
    # edits in the shared worktree as "concurrent changes" and the first one
    # stopped as blocked.
    from src import agent_runs

    drained = []
    sess, _ = _followup_fixture(monkeypatch, drained)

    with agent_runs.track_external(sess.id, source="worker", owner="admin"):
        done = asyncio.run(bg_monitor._run_followup({"id": "job-1", "session_id": sess.id}))

    assert done is False
    assert drained == []


def test_worker_followup_reply_is_handed_up_to_the_parent(monkeypatch):
    # The same day the deferred follow-up finished the work ("done; committed"),
    # but its reply stayed in the worker chat: the parent never heard and the
    # person saw nothing.
    from src import agent_control

    drained = []
    sess, manager = _followup_fixture(monkeypatch, drained)
    handed = []

    async def fake_hand_up(mgr, chat_id, chat, reply, status, owner):
        handed.append((chat_id, reply, status, owner))

    monkeypatch.setattr(agent_control, "_hand_up_when_done", fake_hand_up)

    done = asyncio.run(bg_monitor._run_followup({"id": "job-1", "session_id": sess.id, "status": "done"}))

    assert done is True
    assert drained == ["bg_job-job-1"]
    assert handed == [("worker-1", "worker finished the task", "completed", "admin")]
