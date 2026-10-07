"""Scheduler regressions from the 2026-09-28 diagnostics bundle.

1. ``[loop-lag] event loop blocked 3.48s ... do_executemany <-
   task_scheduler.py:1260 _check_due_tasks`` — the deferral commit for the
   tasks due at 08:00 ran on the event loop. The database half now runs in a
   worker thread with the same dispatch/deferral semantics.

2. "Nightly Odysseus Log Issue Triage", running on chatgpt.com, was cancelled
   at 07:49:34 by a browser heartbeat after five tool calls. The foreground
   gate protects the *local* model lane; a started run on a remote API
   endpoint is no longer preempted. Local/LAN runs still are, and a due task
   that has not started still waits.
"""

import asyncio
import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.orm import declarative_base
from sqlalchemy.pool import QueuePool

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


def _setup_db(make_test_db, monkeypatch, *, record_threads=None):
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
        session_id = Column(String)

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

    maker = make_test_db(base.metadata, poolclass=QueuePool).SessionLocal

    if record_threads is not None:
        def session_local():
            record_threads.append(threading.get_ident())
            return maker()
    else:
        session_local = maker

    monkeypatch.setattr(cd, "SessionLocal", session_local)
    monkeypatch.setattr(cd, "ScheduledTask", ScheduledTask)
    monkeypatch.setattr(cd, "TaskRun", TaskRun)
    return maker, ScheduledTask, TaskRun


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


def _seed(maker, ScheduledTask, task_id="t1", *, next_run=None, name="Nightly Triage"):
    db = maker()
    db.add(ScheduledTask(
        id=task_id, owner="alice", name=name, task_type="llm",
        status="active", trigger_type="event", prompt="audit the logs",
        next_run=next_run, session_id="sess-1",
    ))
    db.commit()
    db.close()


def _load(maker, ScheduledTask, TaskRun, task_id="t1"):
    db = maker()
    try:
        runs = db.query(TaskRun).filter(TaskRun.task_id == task_id).all()
        task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
        return runs, task
    finally:
        db.close()


# ── 1. due-task check off the event loop ───────────────────────────────────


def test_deferral_commit_runs_off_the_event_loop(make_test_db, monkeypatch):
    threads = []
    maker, ScheduledTask, TaskRun = _setup_db(make_test_db, monkeypatch, record_threads=threads)
    was_due = _utcnow() - timedelta(minutes=1)
    _seed(maker, ScheduledTask, next_run=was_due)
    scheduler = _make_scheduler(monkeypatch)

    notes = []
    monkeypatch.setattr(
        "src.task_scheduler._note_scheduler_event",
        lambda task, **kw: notes.append((task, kw)),
    )
    monkeypatch.setattr("src.interactive_gate.has_foreground_activity", lambda: True)

    loop_thread = {}

    async def drive():
        loop_thread["id"] = threading.get_ident()
        await scheduler._check_due_tasks()

    before = _utcnow()
    asyncio.run(drive())

    assert threads, "the due-task query must open a session"
    assert all(t != loop_thread["id"] for t in threads), (
        "the due-task query/commit ran on the event loop thread"
    )
    runs, task = _load(maker, ScheduledTask, TaskRun)
    # Same semantics as before: pushed 15 minutes, no run row, not executing.
    assert runs == []
    assert before + timedelta(minutes=14) < task.next_run < _utcnow() + timedelta(minutes=16)
    assert scheduler._executing == set()
    # The activity note still names the task — from a snapshot, not a live
    # ORM row whose session is already closed.
    assert len(notes) == 1
    noted_task, kw = notes[0]
    assert noted_task.id == "t1" and noted_task.name == "Nightly Triage"
    assert noted_task.owner == "alice" and noted_task.session_id == "sess-1"
    assert kw["reason"] == "foreground_active"
    assert kw["data"]["was_due_at"] == was_due.isoformat()


def test_due_task_is_claimed_and_dispatched_when_idle(make_test_db, monkeypatch):
    maker, ScheduledTask, TaskRun = _setup_db(make_test_db, monkeypatch)
    _seed(maker, ScheduledTask, "due", next_run=_utcnow() - timedelta(minutes=1))
    _seed(maker, ScheduledTask, "busy", next_run=_utcnow() - timedelta(minutes=1))
    _seed(maker, ScheduledTask, "later", next_run=_utcnow() + timedelta(hours=1))
    scheduler = _make_scheduler(monkeypatch)
    scheduler._executing.add("busy")
    monkeypatch.setattr("src.interactive_gate.has_foreground_activity", lambda: False)

    started = []

    async def _fake_execute(task_id, **kw):
        started.append(task_id)

    monkeypatch.setattr(scheduler, "_execute_task", _fake_execute, raising=False)

    async def drive():
        await scheduler._check_due_tasks()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(drive())

    assert started == ["due"]
    assert scheduler._executing == {"busy", "due"}
    _runs, task = _load(maker, ScheduledTask, TaskRun, "due")
    assert task.next_run < _utcnow()  # dispatch does not move next_run here


def test_loop_sleep_computation_is_off_the_event_loop(make_test_db, monkeypatch):
    threads = []
    maker, ScheduledTask, _TaskRun = _setup_db(make_test_db, monkeypatch, record_threads=threads)
    _seed(maker, ScheduledTask, next_run=_utcnow() + timedelta(seconds=30))
    from src.task_scheduler import TaskScheduler

    loop_thread = {}

    async def drive():
        loop_thread["id"] = threading.get_ident()
        return await asyncio.to_thread(TaskScheduler._seconds_until_next_due)

    value = asyncio.run(drive())
    assert 1.0 <= value <= 31.0
    assert threads and all(t != loop_thread["id"] for t in threads)


# ── 2. a started run on a remote model is not preempted ────────────────────

REMOTE = "https://chatgpt.com/backend-api/codex/responses"
LAN = "http://192.168.1.20:8080/v1/chat/completions"


def _patch_llm_executor(scheduler, monkeypatch, endpoint, started, release):
    async def _fake_llm(task, db):
        scheduler._note_run_model_scope(task.id, endpoint)
        started.set()
        await release.wait()
        return "triage done"

    monkeypatch.setattr(scheduler, "_execute_llm_task", _fake_llm, raising=False)


def test_started_remote_run_survives_the_heartbeat_sweep_and_monitor(make_test_db, monkeypatch):
    maker, ScheduledTask, TaskRun = _setup_db(make_test_db, monkeypatch)
    _seed(maker, ScheduledTask)
    scheduler = _make_scheduler(monkeypatch)

    import src.interactive_gate as gate

    counts = []

    async def drive():
        started, release = asyncio.Event(), asyncio.Event()
        _patch_llm_executor(scheduler, monkeypatch, REMOTE, started, release)
        scheduler._executing.add("t1")
        handle = asyncio.create_task(scheduler._execute_task("t1"))
        await asyncio.wait_for(started.wait(), timeout=5)
        # The user comes back: an interactive heartbeat (in-run monitor) and
        # the sweep the heartbeat route fires.
        await gate.mark_browser_activity()
        counts.append(await scheduler.stop_background_tasks_for_foreground(reason="browser heartbeat"))
        await asyncio.sleep(0.8)  # several monitor ticks while the user is active
        assert not handle.done()
        release.set()
        await asyncio.wait_for(handle, timeout=5)

    asyncio.run(drive())

    assert counts == [0]
    runs, _task = _load(maker, ScheduledTask, TaskRun)
    assert len(runs) == 1
    assert runs[0].status == "success"
    assert runs[0].result == "triage done"
    # Consumed with the run, so a later run is classified afresh.
    assert scheduler._remote_model_runs() == {}
    assert "t1" not in scheduler._executing


@pytest.mark.parametrize("endpoint", [LAN, "http://localhost:11434/v1/chat/completions"])
def test_started_local_run_is_still_preempted(make_test_db, monkeypatch, endpoint):
    maker, ScheduledTask, TaskRun = _setup_db(make_test_db, monkeypatch)
    _seed(maker, ScheduledTask)
    scheduler = _make_scheduler(monkeypatch)

    counts = []

    async def drive():
        started, release = asyncio.Event(), asyncio.Event()
        _patch_llm_executor(scheduler, monkeypatch, endpoint, started, release)
        scheduler._executing.add("t1")
        handle = asyncio.create_task(scheduler._execute_task("t1"))
        await asyncio.wait_for(started.wait(), timeout=5)
        counts.append(await scheduler.stop_background_tasks_for_foreground(reason="browser heartbeat"))
        await asyncio.wait_for(handle, timeout=5)

    asyncio.run(drive())

    assert counts == [1]
    runs, task = _load(maker, ScheduledTask, TaskRun)
    assert runs[0].status == "aborted"
    assert "Agamemnon became active" in runs[0].error
    assert task.next_run > _utcnow() + timedelta(minutes=14)


def test_remote_exemption_is_bounded(monkeypatch):
    from src.task_scheduler import TaskScheduler

    scheduler = TaskScheduler.__new__(TaskScheduler)
    assert scheduler._note_run_model_scope("t1", REMOTE) == "api"
    assert scheduler._foreground_preemption_exempt("t1") is True
    # Past the grace window the old preemption applies again.
    scheduler._remote_model_runs()["t1"] = (
        time.monotonic() - TaskScheduler._REMOTE_RUN_FOREGROUND_GRACE.total_seconds() - 1
    )
    assert scheduler._foreground_preemption_exempt("t1") is False


def test_scope_is_reclassified_and_unknown_is_not_exempt():
    from src.task_scheduler import TaskScheduler

    scheduler = TaskScheduler.__new__(TaskScheduler)
    scheduler._note_run_model_scope("t1", REMOTE)
    # The same task later resolved to a LAN box: no longer exempt.
    assert scheduler._note_run_model_scope("t1", LAN) == "lan"
    assert scheduler._foreground_preemption_exempt("t1") is False
    assert scheduler._note_run_model_scope(None, REMOTE) == "unknown"
    assert scheduler._foreground_preemption_exempt("never-started") is False


def test_not_started_remote_task_is_still_deferred(make_test_db, monkeypatch):
    """Only started runs are exempt: a due task still waits for quiet."""
    maker, ScheduledTask, TaskRun = _setup_db(make_test_db, monkeypatch)
    _seed(maker, ScheduledTask, next_run=_utcnow() - timedelta(minutes=1))
    scheduler = _make_scheduler(monkeypatch)
    monkeypatch.setattr("src.task_scheduler._note_scheduler_event", lambda *a, **kw: None)
    monkeypatch.setattr("src.interactive_gate.has_foreground_activity", lambda: True)

    asyncio.run(scheduler._check_due_tasks())

    runs, task = _load(maker, ScheduledTask, TaskRun)
    assert runs == []
    assert task.next_run > _utcnow() + timedelta(minutes=14)
