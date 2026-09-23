import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool


def _database(tmp_path, monkeypatch):
    import core.database as cd

    engine = create_engine(
        f"sqlite:///{tmp_path / 'tiny-pool.db'}",
        connect_args={"check_same_thread": False},
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.15,
    )
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    cd.Base.metadata.create_all(engine)
    # Match core.database.SessionLocal exactly where lifecycle semantics matter:
    # expire_on_commit defaults True, while autoflush is explicitly disabled.
    sessions = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(cd, "SessionLocal", sessions)
    return cd, engine, sessions


def _scheduler(monkeypatch):
    from src.task_scheduler import TaskScheduler

    scheduler = TaskScheduler.__new__(TaskScheduler)
    scheduler._task_handles = {}
    scheduler._executing = set()
    scheduler._executing_lock = asyncio.Lock()
    scheduler._task_defer_counts = {}
    scheduler._pending_notifications = []
    scheduler._last_run_model = None
    scheduler._session_manager = None
    monkeypatch.setattr(scheduler, "_log_to_assistant", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "add_notification", lambda *a, **k: None)
    return scheduler


def _seed(sessions, cd, task_id="task", task_type="action"):
    db = sessions()
    db.add(
        cd.ScheduledTask(
            id=task_id,
            owner="alice",
            name=task_id,
            prompt="do it",
            task_type=task_type,
            action="test",
            status="active",
            trigger_type="event",
            output_target="session",
            endpoint_url="http://model.invalid/v1/chat/completions",
            model="test-model",
        )
    )
    run_id = f"run-{task_id}"
    db.add(cd.TaskRun(id=run_id, task_id=task_id, status="queued"))
    db.commit()
    db.close()
    return run_id


def _read(sessions, cd, task_id="task"):
    db = sessions()
    task = db.query(cd.ScheduledTask).filter_by(id=task_id).one()
    run = db.query(cd.TaskRun).filter_by(task_id=task_id).first()
    db.expunge(task)
    if run:
        db.expunge(run)
    db.close()
    return task, run


def test_jobs_waiting_for_idle_do_not_exhaust_database_pool(tmp_path, monkeypatch):
    import src.interactive_gate as gate

    cd, engine, sessions = _database(tmp_path, monkeypatch)
    scheduler = _scheduler(monkeypatch)
    entered, all_waiting, release = 0, asyncio.Event(), asyncio.Event()
    for n in range(3):
        _seed(sessions, cd, f"task-{n}")

    async def blocked(_label):
        nonlocal entered
        entered += 1
        if entered == 3:
            all_waiting.set()
        await release.wait()

    monkeypatch.setattr(gate, "wait_for_interactive_quiet", blocked)

    async def drive():
        jobs = [
            asyncio.create_task(
                scheduler._execute_task_locked(f"task-{n}", f"run-task-{n}")
            )
            for n in range(3)
        ]
        await asyncio.wait_for(all_waiting.wait(), 1)
        assert engine.pool.checkedout() == 0
        app_db = sessions()
        assert app_db.query(cd.ScheduledTask).count() == 3
        app_db.close()
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    asyncio.run(drive())


def test_direct_cancellation_during_idle_wait_persists_aborted_run(
    tmp_path, monkeypatch
):
    import src.interactive_gate as gate

    cd, engine, sessions = _database(tmp_path, monkeypatch)
    scheduler = _scheduler(monkeypatch)
    run_id = _seed(sessions, cd)
    entered = asyncio.Event()

    async def blocked(_label):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(gate, "wait_for_interactive_quiet", blocked)

    async def drive():
        job = asyncio.create_task(scheduler._execute_task_locked("task", run_id))
        await asyncio.wait_for(entered.wait(), 1)
        assert engine.pool.checkedout() == 0
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)

    asyncio.run(drive())
    _task, run = _read(sessions, cd)
    assert run.status == "aborted"
    assert run.finished_at is not None


def test_action_and_delivery_release_pool_and_persist_success_session(
    tmp_path, monkeypatch
):
    cd, engine, sessions = _database(tmp_path, monkeypatch)
    scheduler, run_id = _scheduler(monkeypatch), _seed(sessions, cd)
    action_entered, action_release = asyncio.Event(), asyncio.Event()
    delivery_entered, delivery_release = asyncio.Event(), asyncio.Event()

    async def action(_task, **_kw):
        action_entered.set()
        await action_release.wait()
        return "finished", True

    original_delivery = scheduler._deliver_task_result

    async def delivery(*args, **kwargs):
        delivery_entered.set()
        await delivery_release.wait()
        return await original_delivery(*args, **kwargs)

    monkeypatch.setattr(scheduler, "_execute_action", action)
    monkeypatch.setattr(scheduler, "_deliver_task_result", delivery)

    async def drive():
        job = asyncio.create_task(
            scheduler._execute_task_locked("task", run_id, gate_foreground=False)
        )
        await asyncio.wait_for(action_entered.wait(), 1)
        assert engine.pool.checkedout() == 0
        action_release.set()
        await asyncio.wait_for(delivery_entered.wait(), 1)
        assert engine.pool.checkedout() == 0
        delivery_release.set()
        await job

    asyncio.run(drive())
    task, run = _read(sessions, cd)
    assert (run.status, run.result, task.run_count) == ("success", "finished", 1)
    assert run.finished_at is not None and task.session_id


@pytest.mark.parametrize(
    "task_type,method_name",
    [
        ("llm", "_execute_llm_task"),
        ("research", "_execute_research_task"),
    ],
)
def test_model_work_releases_pool_during_execution(
    tmp_path, monkeypatch, task_type, method_name
):
    cd, engine, sessions = _database(tmp_path, monkeypatch)
    scheduler = _scheduler(monkeypatch)
    run_id = _seed(sessions, cd, task_type=task_type)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(_task, _db):
        entered.set()
        await release.wait()
        return f"{task_type} complete"

    async def no_delivery(*_args, **_kwargs):
        return None

    monkeypatch.setattr(scheduler, method_name, blocked)
    monkeypatch.setattr(scheduler, "_deliver_task_result", no_delivery)

    async def drive():
        job = asyncio.create_task(
            scheduler._execute_task_locked("task", run_id, gate_foreground=False)
        )
        await asyncio.wait_for(entered.wait(), 1)
        assert engine.pool.checkedout() == 0
        app_db = sessions()
        try:
            assert app_db.query(cd.ScheduledTask).filter_by(id="task").count() == 1
        finally:
            app_db.close()
        release.set()
        await job

    asyncio.run(drive())
    _task, run = _read(sessions, cd)
    assert run.status == "success"
    assert run.result == f"{task_type} complete"


@pytest.mark.parametrize(
    "outcome,expected", [("noop", "skipped"), ("error", "error"), ("cancel", "aborted")]
)
def test_action_terminal_outcomes_are_persisted(
    tmp_path, monkeypatch, outcome, expected
):
    from src.builtin_actions import TaskNoop

    cd, engine, sessions = _database(tmp_path, monkeypatch)
    scheduler, run_id = _scheduler(monkeypatch), _seed(sessions, cd)
    entered = asyncio.Event()

    async def action(_task, **_kw):
        if outcome == "noop":
            raise TaskNoop("nothing changed")
        if outcome == "error":
            return "provider failed", False
        entered.set()
        await asyncio.Event().wait()

    async def no_delivery(*_a, **_k):
        pass

    monkeypatch.setattr(scheduler, "_execute_action", action)
    monkeypatch.setattr(scheduler, "_deliver_task_result", no_delivery)

    async def drive():
        job = asyncio.create_task(
            scheduler._execute_task_locked("task", run_id, gate_foreground=False)
        )
        if outcome == "cancel":
            await asyncio.wait_for(entered.wait(), 1)
            assert engine.pool.checkedout() == 0
            job.cancel()
        await asyncio.gather(job, return_exceptions=True)

    asyncio.run(drive())
    _task, run = _read(sessions, cd)
    assert run.status == expected and run.finished_at is not None


def test_raised_action_error_releases_pool_and_persists_error(tmp_path, monkeypatch):
    cd, engine, sessions = _database(tmp_path, monkeypatch)
    scheduler, run_id = _scheduler(monkeypatch), _seed(sessions, cd)
    entered, release = asyncio.Event(), asyncio.Event()

    async def action(_task, **_kwargs):
        entered.set()
        await release.wait()
        assert engine.pool.checkedout() == 0
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(scheduler, "_execute_action", action)

    async def drive():
        job = asyncio.create_task(
            scheduler._execute_task_locked("task", run_id, gate_foreground=False)
        )
        await asyncio.wait_for(entered.wait(), 1)
        assert engine.pool.checkedout() == 0
        release.set()
        await job

    asyncio.run(drive())
    _task, run = _read(sessions, cd)
    assert run.status == "error"
    assert "upstream exploded" in run.error
    assert run.finished_at is not None


def test_deferred_action_removes_run_and_reschedules(tmp_path, monkeypatch):
    from src.builtin_actions import TaskDeferred

    cd, _engine, sessions = _database(tmp_path, monkeypatch)
    scheduler, run_id = _scheduler(monkeypatch), _seed(sessions, cd)

    async def action(_task, **_kw):
        raise TaskDeferred("quiet", delay_seconds=60)

    monkeypatch.setattr(scheduler, "_execute_action", action)
    asyncio.run(scheduler._execute_task_locked("task", run_id, gate_foreground=False))
    task, run = _read(sessions, cd)
    assert run is None and task.next_run is not None


def test_checkin_accepts_scheduler_scalar_snapshot_and_has_no_checkout(
    tmp_path, monkeypatch
):
    cd, engine, sessions = _database(tmp_path, monkeypatch)
    scheduler = _scheduler(monkeypatch)
    db = sessions()
    db.add(
        cd.CrewMember(
            id="crew",
            owner="alice",
            name="Assistant",
            is_default_assistant=True,
            endpoint_url="http://model.invalid/v1/chat/completions",
            model="test-model",
        )
    )
    db.commit()
    db.close()
    task = SimpleNamespace(
        id="snapshot",
        owner="alice",
        name="Morning check-in",
        prompt="check in",
        crew_member_id="crew",
        endpoint_url=None,
        model=None,
        session_id="existing-session",
        character_id=None,
    )

    async def checkin(received, crew, db_arg, *_args):
        assert received is task or received.id == "snapshot"
        assert crew.id == "crew" and db_arg is None
        assert engine.pool.checkedout() == 0
        return "check-in complete"

    monkeypatch.setattr(scheduler, "_execute_checkin", checkin)
    setup_db = sessions()
    try:
        result = asyncio.run(scheduler._execute_llm_task(task, setup_db))
    finally:
        setup_db.close()
    assert result == "check-in complete"


@pytest.mark.parametrize("existing_session", [False, True])
def test_real_llm_setup_releases_before_tools_headers_and_agent_loop(
    tmp_path, monkeypatch, existing_session
):
    import src.tool_index as tool_index

    cd, engine, sessions = _database(tmp_path, monkeypatch)
    scheduler = _scheduler(monkeypatch)
    _seed(sessions, cd)
    db = sessions()
    db.add(
        cd.CrewMember(
            id="crew",
            owner="alice",
            name="Writer",
            personality="Write well",
            is_default_assistant=False,
            endpoint_url="http://model.invalid/v1/chat/completions",
            model="crew-model",
        )
    )
    task = db.query(cd.ScheduledTask).filter_by(id="task").one()
    task.task_type = "llm"
    task.crew_member_id = "crew"
    if existing_session:
        sess = cd.Session(
            id="existing",
            name="Existing",
            owner="alice",
            endpoint_url=task.endpoint_url,
            model=task.model,
        )
        db.add(sess)
        task.session_id = "existing"
    db.commit()
    db.close()

    class Index:
        def get_tools_for_query(self, *_args, **_kwargs):
            assert engine.pool.checkedout() == 0
            return []

    monkeypatch.setattr(tool_index, "get_tool_index", lambda: Index())

    def headers(*_args, **_kwargs):
        assert engine.pool.checkedout() == 0
        return {"Authorization": "Bearer test"}

    monkeypatch.setattr(scheduler, "_resolve_endpoint_headers", headers)

    async def agent(*_args, **_kwargs):
        assert engine.pool.checkedout() == 0
        return "model result"

    monkeypatch.setattr(scheduler, "_run_agent_loop", agent)

    async def guarded(_key, callback):
        return await callback()

    monkeypatch.setattr(scheduler, "_guarded_model_call", guarded)

    setup_db = sessions()
    task = setup_db.query(cd.ScheduledTask).filter_by(id="task").one()
    try:
        assert (
            asyncio.run(scheduler._execute_llm_task(task, setup_db)) == "model result"
        )
    finally:
        setup_db.close()
    persisted, _run = _read(sessions, cd)
    assert persisted.session_id == (
        "existing" if existing_session else persisted.session_id
    )
    assert persisted.session_id


def test_research_releases_pool_and_persists_output_session(tmp_path, monkeypatch):
    import src.deep_research as deep_research
    import src.research_handler as research_handler

    cd, engine, sessions = _database(tmp_path, monkeypatch)
    scheduler = _scheduler(monkeypatch)
    ensured = []

    class SessionManager:
        def ensure_task_session(self, *args, **kwargs):
            assert engine.pool.checkedout() == 0
            ensured.append((args, kwargs))

    scheduler._session_manager = SessionManager()
    _seed(sessions, cd)
    db = sessions()
    stored = db.query(cd.ScheduledTask).filter_by(id="task").one()
    stored.task_type = "research"
    db.commit()
    task = SimpleNamespace(
        **{
            column.key: getattr(stored, column.key)
            for column in stored.__mapper__.column_attrs
        }
    )
    db.close()

    class FakeResearcher:
        def __init__(self, **_kwargs):
            self.findings = []

        async def research(self, _prompt):
            assert engine.pool.checkedout() == 0
            return "research report"

        def get_stats(self):
            return {}

    monkeypatch.setattr(deep_research, "DeepResearcher", FakeResearcher)
    monkeypatch.setattr(research_handler, "RESEARCH_DATA_DIR", tmp_path / "research")
    monkeypatch.setattr(scheduler, "_resolve_endpoint_headers", lambda *_a, **_k: {})

    async def guarded(_key, callback):
        return await callback()

    monkeypatch.setattr(scheduler, "_guarded_model_call", guarded)

    setup_db = sessions()
    try:
        result = asyncio.run(scheduler._execute_research_task(task, setup_db))
    finally:
        setup_db.close()
    assert result == "research report"
    persisted, _run = _read(sessions, cd)
    assert persisted.session_id
    assert (tmp_path / "research" / f"{persisted.session_id}.json").exists()
    assert len(ensured) == 1
    assert ensured[0][0][0] == persisted.session_id
    assert ensured[0][0][1] == "[Research] task"
    assert ensured[0][1]["owner"] == "alice"
