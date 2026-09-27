"""A background run swept away by a foreground request is a preemption, not a stop.

Production log (2026-09-27):

    Stopped 2 background scheduler task(s): foreground request GET /api/email/list
    Task 'Email Tags' Stopped by user

One run, counted twice (cancel + mark-aborted), labelled as if the user had
pressed Stop, and given its normal next slot instead of the 15-minute retry the
in-run foreground monitor gives the very same situation.
"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from src.task_scheduler import _utcnow


@pytest.fixture(autouse=True)
def _reset_foreground_gate():
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


def _make_scheduler(monkeypatch):
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

    async def _noop_deliver(*a, **kw):
        return None

    monkeypatch.setattr(scheduler, "_deliver_task_result", _noop_deliver, raising=False)
    monkeypatch.setattr(scheduler, "_log_to_assistant", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(scheduler, "add_notification", lambda *a, **kw: None, raising=False)
    return scheduler


def _seed(session_local, ScheduledTask, *, next_run=None):
    db = session_local()
    db.add(ScheduledTask(
        id="tags", owner="alice", name="Email Tags", task_type="action",
        action="email_tags", status="active", trigger_type="event",
        next_run=next_run,
    ))
    db.commit()
    db.close()


def _load(session_local, ScheduledTask, TaskRun):
    db = session_local()
    try:
        runs = db.query(TaskRun).filter(TaskRun.task_id == "tags").all()
        task = db.query(ScheduledTask).filter(ScheduledTask.id == "tags").first()
        return runs, task
    finally:
        db.close()


REASON = "foreground request GET /api/email/list"


def test_sweep_preempts_a_running_task_once_with_the_retry(tmp_path, monkeypatch):
    session_local, ScheduledTask, TaskRun = _setup_db(tmp_path, monkeypatch)
    _seed(session_local, ScheduledTask)
    scheduler = _make_scheduler(monkeypatch)

    started = asyncio.Event()

    async def _slow_action(task, run_id=None, *, manual=False):
        started.set()
        await asyncio.sleep(30)
        return "never", True

    monkeypatch.setattr(scheduler, "_execute_action", _slow_action, raising=False)

    counts = []

    async def drive():
        scheduler._executing.add("tags")
        handle = asyncio.create_task(scheduler._execute_task("tags"))
        await asyncio.wait_for(started.wait(), timeout=5)
        # Two foreground requests land before the run has unwound.
        counts.append(await scheduler.stop_background_tasks_for_foreground(reason=REASON))
        counts.append(await scheduler.stop_background_tasks_for_foreground(reason=REASON))
        await asyncio.wait_for(handle, timeout=5)

    before = _utcnow()
    asyncio.run(drive())

    # One run, counted once.
    assert counts == [1, 0]

    runs, task = _load(session_local, ScheduledTask, TaskRun)
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "aborted"
    assert "Stopped by user" not in run.error
    assert "Odysseus became active" in run.error
    assert "GET /api/email/list" in run.error
    assert "retrying in 15 min" in run.error
    # The short retry, not the task's normal next occurrence (None for an
    # event trigger).
    assert task.next_run is not None
    assert before + timedelta(minutes=14) < task.next_run < _utcnow() + timedelta(minutes=16)

    # The mark is consumed so the task's next run starts clean.
    assert scheduler._foreground_preemptions() == {}
    assert "tags" not in scheduler._executing

    # Diagnostics classify it as a foreground interrupt, not a user stop.
    from src.runtime_introspection import _outcome

    assert _outcome(run)["abort_cause"] == "foreground_interrupt"


def test_sweep_preempts_a_queued_task_with_the_same_label(tmp_path, monkeypatch):
    """Cancelled while still waiting for idle — the outer dispatcher's handler."""
    session_local, ScheduledTask, TaskRun = _setup_db(tmp_path, monkeypatch)
    overdue = _utcnow() - timedelta(minutes=1)
    _seed(session_local, ScheduledTask, next_run=overdue)
    scheduler = _make_scheduler(monkeypatch)

    async def _never(task, run_id=None, *, manual=False):
        raise AssertionError("a queued run must not start")

    monkeypatch.setattr(scheduler, "_execute_action", _never, raising=False)

    import src.interactive_gate as gate

    counts = []

    async def drive():
        await gate.mark_browser_activity()
        scheduler._executing.add("tags")
        handle = asyncio.create_task(scheduler._execute_task("tags"))
        for _ in range(200):
            runs, _t = _load(session_local, ScheduledTask, TaskRun)
            if runs and "idle" in (runs[0].result or ""):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("run never reached the idle wait")
        counts.append(await scheduler.stop_background_tasks_for_foreground(reason=REASON))
        with pytest.raises(asyncio.CancelledError):
            await handle

    asyncio.run(drive())

    assert counts == [1]
    runs, task = _load(session_local, ScheduledTask, TaskRun)
    assert runs[0].status == "aborted"
    assert "Odysseus became active" in runs[0].error
    assert "retrying in 15 min" in runs[0].error
    assert task.next_run > _utcnow() + timedelta(minutes=14)
    assert scheduler._foreground_preemptions() == {}


def test_user_stop_is_still_a_user_stop(tmp_path, monkeypatch):
    """The preemption mark must not leak onto a real Stop."""
    session_local, ScheduledTask, TaskRun = _setup_db(tmp_path, monkeypatch)
    _seed(session_local, ScheduledTask)
    scheduler = _make_scheduler(monkeypatch)

    started = asyncio.Event()

    async def _slow_action(task, run_id=None, *, manual=False):
        started.set()
        await asyncio.sleep(30)
        return "never", True

    monkeypatch.setattr(scheduler, "_execute_action", _slow_action, raising=False)

    async def drive():
        scheduler._executing.add("tags")
        handle = asyncio.create_task(scheduler._execute_task("tags"))
        await asyncio.wait_for(started.wait(), timeout=5)
        assert await scheduler.stop_task("tags") is True
        await asyncio.wait_for(handle, timeout=5)

    asyncio.run(drive())

    runs, task = _load(session_local, ScheduledTask, TaskRun)
    assert runs[0].status == "aborted"
    assert runs[0].error == "Stopped by user"
    # Event trigger + user stop: no automatic retry.
    assert task.next_run is None


def test_pause_message_without_a_distinct_reason():
    from src.task_scheduler import TaskScheduler

    assert TaskScheduler._foreground_pause_message() == (
        "Paused because Odysseus became active; retrying in 15 min"
    )
    # The sweep's default reason would otherwise repeat itself.
    assert TaskScheduler._foreground_pause_message("Odysseus became active") == (
        "Paused because Odysseus became active; retrying in 15 min"
    )
