"""Run now must actually run.

Three regressions are covered here:

1. A manually triggered run used to be pushed through the same "wait until
   Odysseus is idle" gate as scheduled work, and then cancelled by the
   foreground-activity sweep. Since the user is by definition active in the tab
   they clicked in, the run sat at "Queued — waiting for Odysseus to be idle…"
   and/or was aborted moments after starting.

2. That idle wait happened *inside* the single run slot, so a background task
   parked on the gate blocked every other task behind it.

3. An agent stream that failed upstream (e.g. HTTP 401 from the provider) was
   recorded as a *successful* run with "(no output)", because SSE `event: error`
   frames were dropped by the result parser.
"""

import asyncio

import pytest
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker


@pytest.fixture(autouse=True)
def _reset_foreground_gate():
    """The gate keeps process-global activity timestamps. Left set, they make
    the *next* test wait out the browser-active TTL (which is what the bug under
    test does to real scheduled runs), so clear them around every test."""
    import src.interactive_gate as gate

    def _clear():
        gate._LAST_BROWSER_ACTIVITY = 0.0
        gate._LAST_ACTIVITY = 0.0
        gate._ACTIVE_REQUESTS = 0

    _clear()
    yield
    _clear()


def _setup_db(tmp_path, monkeypatch):
    import core.database as cd

    base = declarative_base()

    class ScheduledTask(base):
        __tablename__ = "scheduled_tasks"

        id = Column(String, primary_key=True)
        owner = Column(String)
        name = Column(String)
        task_type = Column(String, default="llm")
        action = Column(String)
        status = Column(String, default="active")
        trigger_type = Column(String, default="event")
        prompt = Column(Text)
        next_run = Column(DateTime)
        last_run = Column(DateTime)
        run_count = Column(Integer, default=0)
        output_target = Column(String, default="session")
        notifications_enabled = Column(Boolean, default=True)
        then_task_id = Column(String)

    class TaskRun(base):
        __tablename__ = "task_runs"

        id = Column(String, primary_key=True)
        task_id = Column(String)
        started_at = Column(DateTime)
        finished_at = Column(DateTime)
        status = Column(String)
        result = Column(Text)
        error = Column(Text)
        model = Column(String)

    engine = create_engine(f"sqlite:///{tmp_path / 'tasks.db'}")
    base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(cd, "SessionLocal", session_local)
    monkeypatch.setattr(cd, "ScheduledTask", ScheduledTask)
    monkeypatch.setattr(cd, "TaskRun", TaskRun)
    return session_local, ScheduledTask, TaskRun


def _make_scheduler():
    from src.task_scheduler import TaskScheduler

    scheduler = TaskScheduler.__new__(TaskScheduler)
    scheduler._executing = set()
    scheduler._executing_lock = asyncio.Lock()
    scheduler._run_semaphore = asyncio.Semaphore(1)
    scheduler._task_handles = {}
    scheduler._concurrency_cap = 1
    scheduler._task_defer_counts = {}
    scheduler._manual_runs = {}
    scheduler._pending_notifications = []
    scheduler._last_run_model = None
    return scheduler


def _stub_side_effects(scheduler, monkeypatch):
    async def _noop_deliver(*a, **kw):
        return None

    monkeypatch.setattr(scheduler, "_deliver_task_result", _noop_deliver, raising=False)
    monkeypatch.setattr(scheduler, "_log_to_assistant", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(scheduler, "add_notification", lambda *a, **kw: None, raising=False)


def _seed_task(session_local, ScheduledTask, task_id="t1"):
    db = session_local()
    db.add(ScheduledTask(
        id=task_id,
        owner="alice",
        name="Manual Task",
        task_type="action",
        action="memory_consolidation",
        status="active",
        trigger_type="event",
    ))
    db.commit()
    db.close()


def test_manual_run_does_not_wait_for_idle(tmp_path, monkeypatch):
    """The browser is active — a manual run must still execute."""
    session_local, ScheduledTask, TaskRun = _setup_db(tmp_path, monkeypatch)
    _seed_task(session_local, ScheduledTask)

    import src.interactive_gate as gate

    scheduler = _make_scheduler()
    _stub_side_effects(scheduler, monkeypatch)

    ran = asyncio.Event()

    async def _fake_action(task, run_id=None, *, manual=False):
        ran.set()
        assert manual is True
        return "did the thing", True

    monkeypatch.setattr(scheduler, "_execute_action", _fake_action, raising=False)

    async def drive():
        # The user is looking at Odysseus right now.
        await gate.mark_browser_activity()
        assert gate.has_foreground_activity() is True

        assert await scheduler.run_task_now("t1", manual=True) is True
        await asyncio.wait_for(ran.wait(), timeout=5)
        # Let the run finish writing its row.
        for _ in range(200):
            if "t1" not in scheduler._executing:
                break
            await asyncio.sleep(0.01)

    asyncio.run(drive())

    db = session_local()
    try:
        run = db.query(TaskRun).filter(TaskRun.task_id == "t1").first()
        assert run.status == "success"
        assert run.result == "did the thing"
    finally:
        db.close()


def test_scheduled_run_still_waits_for_idle(tmp_path, monkeypatch):
    """The gate is intact for automatic dispatch — only manual runs skip it."""
    session_local, ScheduledTask, TaskRun = _setup_db(tmp_path, monkeypatch)
    _seed_task(session_local, ScheduledTask)

    import src.interactive_gate as gate

    scheduler = _make_scheduler()
    _stub_side_effects(scheduler, monkeypatch)

    ran = asyncio.Event()

    async def _fake_action(task, run_id=None, *, manual=False):
        ran.set()
        return "did the thing", True

    monkeypatch.setattr(scheduler, "_execute_action", _fake_action, raising=False)

    async def drive():
        await gate.mark_browser_activity()
        handle = asyncio.create_task(scheduler._execute_task("t1"))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(ran.wait()), timeout=0.5)
        handle.cancel()
        try:
            await handle
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert not ran.is_set()


def test_foreground_sweep_leaves_manual_runs_alone(tmp_path, monkeypatch):
    """The heartbeat's "stop background work" sweep must not kill Run now."""
    session_local, ScheduledTask, TaskRun = _setup_db(tmp_path, monkeypatch)
    _seed_task(session_local, ScheduledTask)

    scheduler = _make_scheduler()
    _stub_side_effects(scheduler, monkeypatch)

    started = asyncio.Event()
    finished = asyncio.Event()

    async def _slow_action(task, run_id=None, *, manual=False):
        started.set()
        await asyncio.sleep(0.3)
        finished.set()
        return "survived", True

    monkeypatch.setattr(scheduler, "_execute_action", _slow_action, raising=False)

    async def drive():
        assert await scheduler.run_task_now("t1", manual=True) is True
        await asyncio.wait_for(started.wait(), timeout=5)
        # This is what every browser heartbeat and UI request triggers.
        stopped = await scheduler.stop_background_tasks_for_foreground(reason="test")
        assert stopped == 0
        await asyncio.wait_for(finished.wait(), timeout=5)
        for _ in range(200):
            if "t1" not in scheduler._executing:
                break
            await asyncio.sleep(0.01)

    asyncio.run(drive())

    db = session_local()
    try:
        run = db.query(TaskRun).filter(TaskRun.task_id == "t1").first()
        assert run.status == "success"
        assert run.result == "survived"
    finally:
        db.close()


def test_background_task_does_not_hold_the_slot_while_waiting_for_idle(tmp_path, monkeypatch):
    """A scheduled run parked on the idle gate used to keep the single model
    slot, so every later task — Run now included — queued behind a run that
    could not start until the user walked away."""
    session_local, ScheduledTask, TaskRun = _setup_db(tmp_path, monkeypatch)

    db = session_local()
    db.add(ScheduledTask(
        id="bg", owner="alice", name="Background", task_type="llm",
        status="active", trigger_type="event",
    ))
    db.commit()
    db.close()

    import src.interactive_gate as gate

    scheduler = _make_scheduler()
    _stub_side_effects(scheduler, monkeypatch)

    async def _never_reached(task, db):
        raise AssertionError("the gated background task should not have started")

    monkeypatch.setattr(scheduler, "_execute_llm_task", _never_reached, raising=False)

    async def drive():
        await gate.mark_browser_activity()
        handle = asyncio.create_task(scheduler._execute_task("bg"))
        await asyncio.sleep(0.3)
        # Still waiting for quiet — but the run slot must be free for others.
        assert not scheduler._run_semaphore.locked()
        handle.cancel()
        try:
            await handle
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())


def test_manual_refcount_released_after_run(tmp_path, monkeypatch):
    session_local, ScheduledTask, TaskRun = _setup_db(tmp_path, monkeypatch)
    _seed_task(session_local, ScheduledTask)

    scheduler = _make_scheduler()
    _stub_side_effects(scheduler, monkeypatch)

    async def _fake_action(task, run_id=None, *, manual=False):
        return "ok", True

    monkeypatch.setattr(scheduler, "_execute_action", _fake_action, raising=False)

    async def drive():
        assert await scheduler.run_task_now("t1", manual=True) is True
        for _ in range(300):
            if not scheduler._manual_runs and "t1" not in scheduler._executing:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"manual refcount leaked: {scheduler._manual_runs}")

    asyncio.run(drive())


_ERROR_FRAME = (
    'event: error\n'
    'data: {"status": 401, "text": "ChatGPT Subscription credentials expired '
    'or were rejected. Reconnect the provider.", "raw": "{\\"detail\\":\\"Unauthorized\\"}"}\n\n'
)


def _stub_llm_task_deps(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: [] if key == "disabled_tools" else default,
    )
    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: None)
    monkeypatch.setattr("src.task_endpoint.resolve_task_candidates", lambda **kw: [])

    async def _stub_stream(**kwargs):
        yield _ERROR_FRAME

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", _stub_stream)
    return SimpleNamespace(
        crew_member_id=None, endpoint_url="http://ep/v1", model="m",
        session_id="s", owner="admin", prompt="run the report",
        name="job", max_steps=5, character_id=None,
    )


async def test_provider_error_triggers_the_fallback_chain(monkeypatch):
    """A 401 used to be swallowed: the run "succeeded" with "(no output)" and
    the fallback endpoints were never tried. Now the error propagates out of the
    agent loop, so the simple-call fallback gets its chance."""
    task = _stub_llm_task_deps(monkeypatch)

    import src.task_endpoint as _te

    async def _fallback(messages, **kw):
        return "recovered on the fallback endpoint"

    monkeypatch.setattr(_te, "task_llm_call_async", _fallback)

    from src.task_scheduler import TaskScheduler

    result = await TaskScheduler(session_manager=None)._execute_llm_task(task, db=None)
    assert result == "recovered on the fallback endpoint"


async def test_provider_error_with_no_fallback_fails_the_run(monkeypatch):
    """When nothing can serve the task, the run must error — not report success
    with an empty result."""
    task = _stub_llm_task_deps(monkeypatch)

    import src.task_endpoint as _te

    async def _fallback(messages, **kw):
        return ""

    monkeypatch.setattr(_te, "task_llm_call_async", _fallback)

    from src.task_scheduler import TaskScheduler

    with pytest.raises(RuntimeError, match="no output"):
        await TaskScheduler(session_manager=None)._execute_llm_task(task, db=None)


def test_parse_stream_error_reads_sse_error_frame():
    from src.task_scheduler import _parse_stream_error

    chunk = (
        'event: error\n'
        'data: {"status": 401, "text": "ChatGPT Subscription credentials expired '
        'or were rejected. Reconnect the provider.", "raw": "{\\"detail\\":\\"Unauthorized\\"}"}\n\n'
    )
    msg = _parse_stream_error(chunk)
    assert "credentials expired" in msg
    assert "401" in msg

    assert _parse_stream_error('event: error\ndata: {"status": 503}\n\n') == (
        "Model stream failed with HTTP 503"
    )
    assert _parse_stream_error("event: error\n\n") == ""
