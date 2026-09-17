"""Multi-chat run visibility, per-sub-agent stop, and sub-agent tool policy.

Before these changes only the browser tab that sent a message knew a chat was
running; a sub-agent could only be stopped by stopping its parent turn; and a
headless child skipped the operator's global disabled-tools setting.
"""
import asyncio
import json

import pytest

from src import agent_activity as act
from src import agent_runs, constants
from src import headless_agent


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    act._reset_for_tests()
    agent_runs._RUNS.clear()
    agent_runs._EXTERNAL.clear()
    agent_runs._FINISHED.clear()
    yield
    act._reset_for_tests()
    agent_runs._RUNS.clear()
    agent_runs._EXTERNAL.clear()
    agent_runs._FINISHED.clear()


def _sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


async def test_streamed_runs_are_listed_running_then_finished_and_scoped():
    release = asyncio.Event()

    async def gen():
        yield _sse({"delta": "hi"})
        await release.wait()
        yield "data: [DONE]\n\n"

    agent_runs.start("chat-a", gen(), owner="alice")
    await asyncio.sleep(0)
    rows = agent_runs.list_runs({"chat-a"})
    assert [(r["session_id"], r["status"]) for r in rows] == [("chat-a", "running")]
    assert agent_runs.list_runs({"someone-elses"}) == []

    release.set()
    await agent_runs._RUNS["chat-a"].task
    row = agent_runs.list_runs({"chat-a"})[0]
    assert row["status"] == "done" and row["finished_at"] >= row["started_at"]


def test_background_turns_mark_the_chat_busy():
    with agent_runs.track_external("chat-b", source="bg_job", owner="alice"):
        assert agent_runs.is_busy("chat-b") and not agent_runs.is_active("chat-b")
        assert agent_runs.list_runs({"chat-b"})[0]["status"] == "running"
    assert not agent_runs.is_busy("chat-b")
    assert agent_runs.list_runs({"chat-b"})[0]["status"] == "done"


class _Sess:
    id = "child"
    endpoint_url = "http://llm.test/v1"
    model = "m"
    headers = {}
    owner = "alice"
    context_length = 0


async def test_request_stop_ends_one_subagent_and_keeps_its_partial(monkeypatch):
    started = asyncio.Event()

    async def slow_loop(url, model, messages, **kwargs):
        yield _sse({"delta": "Checked a.py so far"})
        started.set()
        await asyncio.sleep(30)
        yield _sse({"delta": " and more"})

    import src.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "stream_agent_loop", slow_loop)
    outcome = {}
    task = asyncio.ensure_future(headless_agent.run_headless(
        _Sess(), [{"role": "user", "content": "go"}], run_id="session-abc",
        activity_session_id="parent", outcome=outcome))
    await started.wait()
    assert "session-abc" in headless_agent.running_ids()
    assert headless_agent.steering_run_id("child") == "session-abc"
    assert headless_agent.request_stop("session-abc") is True
    text, _events = await asyncio.wait_for(task, 5)
    assert outcome == {"stopped": True}
    assert text.startswith("Checked a.py so far") and "stopped by the user" in text
    assert "session-abc" not in headless_agent.running_ids()
    assert headless_agent.steering_run_id("child") is None
    assert headless_agent.request_stop("session-abc") is False


async def test_headless_children_get_the_global_disabled_tools(monkeypatch):
    seen = {}

    async def fake_loop(url, model, messages, **kwargs):
        seen.update(kwargs)
        yield "data: [DONE]\n\n"

    import src.agent_loop as agent_loop
    import src.settings as settings
    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)
    monkeypatch.setattr(settings, "get_setting",
                        lambda key, default=None: ["bash", "web_fetch"] if key == "disabled_tools" else default)

    # The owner's admin status is pinned rather than resolved from the
    # environment. owner_baseline_disabled_tools now also carries the public-user
    # blocklist, and owner_is_admin_or_single_user fails closed, so without this
    # the assertion below would be measuring whether the test runner happens to
    # have a loadable auth manager instead of what this test is about: the
    # operator's global `disabled_tools` reaching a headless child.
    import src.tool_security as tool_security
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: True)

    await headless_agent.run_headless(_Sess(), [], run_id=None)
    assert {"bash", "web_fetch", "send_to_session"} <= set(seen["disabled_tools"])

    # A chat continuing itself (disabled_tools=None) still honours the operator.
    await headless_agent.run_headless(_Sess(), [], disabled_tools=None)
    assert set(seen["disabled_tools"]) == {"bash", "web_fetch"}


@pytest.mark.parametrize("frame", [
    'event: error\ndata: {"status": 401, "text": "credentials expired"}\n\n',
    'data: {"type": "stream_error", "status": 503, "error": "provider unavailable"}\n\n',
])
async def test_headless_promotes_terminal_stream_errors(monkeypatch, frame):
    async def failed_loop(url, model, messages, **kwargs):
        yield frame

    import src.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "stream_agent_loop", failed_loop)
    with pytest.raises(headless_agent.HeadlessStreamError) as raised:
        await headless_agent.run_headless(_Sess(), [], run_id="failed-worker")
    assert raised.value.status in {401, 503}
    assert "failed" in str(raised.value).lower()
    assert "failed-worker" not in headless_agent.running_ids()


async def test_headless_refreshes_session_auth_before_streaming(monkeypatch):
    seen = {}
    async def healthy_loop(url, model, messages, **kwargs):
        yield "data: [DONE]\n\n"

    import src.agent_loop as agent_loop
    import routes.chat_helpers as chat_helpers
    monkeypatch.setattr(agent_loop, "stream_agent_loop", healthy_loop)
    monkeypatch.setattr(chat_helpers, "resolve_session_auth",
                        lambda sess, session_id, owner=None: seen.update(
                            session_id=session_id, owner=owner))
    await headless_agent.run_headless(_Sess(), [])
    assert seen == {"session_id": "child", "owner": "alice"}


async def test_headless_steer_id_does_not_overwrite_its_wrapper_activity_run(monkeypatch):
    """The dashboard's worker record and the loop's telemetry are distinct.

    Reusing the wrapper id for ``stream_agent_loop(... run_id=...)`` turns its
    source into ``odysseus`` and replaces the wrapper summary.  Steering needs
    the wrapper id for queue isolation, but activity accounting must retain
    both records.
    """
    seen = {}

    async def fake_loop(_url, _model, _messages, **kwargs):
        seen.update(kwargs)
        yield "data: [DONE]\n\n"

    import src.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)
    wrapper = act.run_started(
        "child", "session", "Worker · audit", run_id="worker-run",
        owner="alice", data={"target_session": "child", "mode": "agent"},
    )
    await headless_agent.run_headless(
        _Sess(), [], run_id=wrapper, activity_session_id="child", source="session", owner="alice",
    )
    record = act.get_run(wrapper)
    assert seen["steer_run_id"] == wrapper
    assert record["source"] == "session"
    assert record["summary"]["target_session"] == "child"


async def test_dashboard_steer_binds_to_and_is_drained_by_headless_wrapper(monkeypatch):
    """The loop's telemetry id is not the headless wrapper steering id."""
    started = asyncio.Event()
    release = asyncio.Event()
    drained = []

    async def fake_loop(_url, _model, _messages, **kwargs):
        started.set()
        await release.wait()
        from src import agent_control
        drained.extend(agent_control.drain_steer("child", run_id=kwargs["steer_run_id"]))
        yield "data: [DONE]\n\n"

    import src.agent_loop as agent_loop
    from src import agent_control
    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)
    task = asyncio.create_task(headless_agent.run_headless(_Sess(), [], run_id="wrapper-run"))
    await started.wait()
    steer = agent_control.steer("child", "report only the findings")
    assert steer["run_id"] == "wrapper-run"
    release.set()
    await task
    assert drained == ["report only the findings"]


async def test_headless_stop_cancels_only_its_undrained_steer(monkeypatch):
    started = asyncio.Event()

    async def slow_loop(_url, _model, _messages, **_kwargs):
        started.set()
        await asyncio.sleep(30)
        yield "data: [DONE]\n\n"

    import src.agent_loop as agent_loop
    from src import agent_control
    monkeypatch.setattr(agent_loop, "stream_agent_loop", slow_loop)
    task = asyncio.create_task(headless_agent.run_headless(_Sess(), [], run_id="stop-wrapper"))
    await started.wait()
    steer = agent_control.steer("child", "do not leak into a later run")
    assert headless_agent.request_stop("stop-wrapper")
    await task
    row = next(row for row in agent_control.steer_history("child") if row["id"] == steer["id"])
    assert row["state"] == "cancelled"


async def test_a_non_admin_headless_child_is_denied_the_public_blocklist(monkeypatch):
    """The same merge point also has to stop a child being handed tools the
    execution gate would refuse — see tests/test_non_admin_phantom_tools.py."""
    seen = {}

    async def fake_loop(url, model, messages, **kwargs):
        seen.update(kwargs)
        yield "data: [DONE]\n\n"

    import src.agent_loop as agent_loop
    import src.settings as settings
    import src.tool_security as tool_security
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS
    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: [] if key == "disabled_tools" else default)
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: False)

    await headless_agent.run_headless(_Sess(), [], disabled_tools=None)
    assert NON_ADMIN_BLOCKED_TOOLS <= set(seen["disabled_tools"])


def test_bg_job_is_a_run_from_launch_and_kill_closes_it(monkeypatch, tmp_path):
    from src import bg_jobs

    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(bg_jobs, "_kill", lambda pid: None)

    class _Proc:
        pid = 4242

    monkeypatch.setattr(bg_jobs.subprocess, "Popen", lambda *a, **k: _Proc())
    rec = bg_jobs.launch("sleep 100", "chat-c")
    run = act.get_run(f"bg_job-{rec['id']}")
    assert run["status"] == "running" and run["summary"]["job_id"] == rec["id"]
    bg_jobs.kill(rec["id"])
    assert act.get_run(f"bg_job-{rec['id']}")["status"] == "cancelled"


# ── a worker that ran out of rounds is not a worker that finished ─────────
#
# The round cap is a budget, and spending it mid-task is a normal outcome: the
# worker did real work and simply stopped. Reporting that as `completed` told
# the dashboard it was fine and handed the parent chat a result with nothing in
# it to act on.


class _Worker:
    id, name = "w-1", "Runner"


def _manager(sessions):
    class _M:
        def get_session(self, sid):
            return sessions.get(sid)

        def save_sessions(self):
            pass
    return _M()


class _Chat:
    def __init__(self, sid):
        self.id, self.name, self.model = sid, sid, "m"
        self.messages = []

    def add_message(self, message):
        self.messages.append(message)

    def get_context_messages(self):
        return []


async def test_a_cut_off_worker_is_reported_incomplete_not_completed(monkeypatch):
    from src import agent_control
    import src.agent_tools.session_tools as session_tools
    import src.ai_interaction as ai_interaction
    import src.headless_agent as headless

    worker_chat = _Chat("w-1")
    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: _manager({"w-1": worker_chat}))
    monkeypatch.setattr(session_tools, "_new_child_session", lambda *a, **k: (worker_chat, None))

    async def fake_headless(sess, messages, **kwargs):
        kwargs["outcome"]["rounds_exhausted"] = True
        kwargs["outcome"]["rounds"] = 6
        return "Read five files.\n\n(ran out of rounds: ...)", [{"tool": "read_file"}]

    monkeypatch.setattr(headless, "run_headless", fake_headless)

    rec = await agent_control.launch_worker(owner="alice", task="audit the handler")
    await agent_control._WORKERS[rec["run_id"]]

    assert act.get_run(rec["run_id"])["status"] == "incomplete"
    # The partial work is still what gets saved — the status is the warning.
    assert worker_chat.messages[-1].content.startswith("Read five files.")
    # The budget the run was cut off by is reported with it, so "ran out of
    # rounds" can be read against a number without reopening the loadout.
    from src.agent_profiles import DEFAULT_ROUNDS

    assert rec["max_rounds"] == DEFAULT_ROUNDS


async def test_the_parent_is_told_the_worker_was_cut_off(monkeypatch):
    from src import agent_control

    parent = _Chat("parent")
    # Mid-turn, so the hand-off stops at persisting the message for the next
    # turn — which is exactly the text under test.
    monkeypatch.setattr(agent_runs, "is_busy", lambda sid: True)

    await agent_control._hand_off(_manager({"parent": parent}), "parent", _Worker(),
                                  "audit the handler", "Read five files.", "incomplete", "alice")

    inject = parent.messages[-1].content
    assert "[Worker Runner ran out of rounds]" in inject
    assert "partial" in inject and "pick the task up from where it stopped" in inject


async def test_a_finished_worker_hand_off_is_unchanged(monkeypatch):
    from src import agent_control

    parent = _Chat("parent")
    monkeypatch.setattr(agent_runs, "is_busy", lambda sid: True)

    await agent_control._hand_off(_manager({"parent": parent}), "parent", _Worker(),
                                  "audit the handler", "All five files are clean.", "completed", "alice")

    inject = parent.messages[-1].content
    assert "[Worker Runner finished]" in inject and "cut off" not in inject


async def test_the_chat_that_started_a_worker_sees_it_start_and_finish(monkeypatch):
    """The agent strip above the composer is driven by the PARENT chat's feed.

    A worker's run_started/run_finished are filed under the worker's own chat,
    so without these two parent-side events the strip drew a row that never
    resolved and was then reconciled away as interrupted — which is why
    sub-agents stopped appearing in the chat that launched them.
    """
    from src import agent_control
    import src.agent_tools.session_tools as session_tools
    import src.ai_interaction as ai_interaction
    import src.headless_agent as headless

    parent_chat, worker_chat = _Chat("parent"), _Chat("w-9")
    sessions = {"parent": parent_chat, "w-9": worker_chat}
    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: _manager(sessions))
    monkeypatch.setattr(session_tools, "_new_child_session", lambda *a, **k: (worker_chat, None))
    monkeypatch.setattr(agent_runs, "is_busy", lambda sid: True)

    async def fake_headless(sess, messages, **kwargs):
        kwargs["outcome"]["rounds_exhausted"] = True
        return "Looked at four files.", [{"tool": "read_file"}]

    monkeypatch.setattr(headless, "run_headless", fake_headless)

    rec = await agent_control.launch_worker(
        owner="alice", task="audit the notes UI", parent_session="parent")
    await agent_control._WORKERS[rec["run_id"]]

    parent_events = [ev for ev in act.history("parent") if ev["run_id"] == rec["run_id"]]
    kinds = [ev["kind"] for ev in parent_events]
    assert "message" in kinds and "status" in kinds

    closing = next(ev for ev in parent_events if ev["kind"] == "status")
    assert closing["data"]["status"] == "incomplete"
    assert closing["data"]["rounds_exhausted"] is True
    assert f"{rec['max_rounds']}-round budget" in closing["title"]

    # And the parent can list the run, which is what stops the strip's
    # reconcile pass from deciding the run it could not find was interrupted.
    listed = act.list_runs(session_id="parent")
    assert [r["run_id"] for r in listed] == [rec["run_id"]]
    assert listed[0]["status"] == "incomplete"
