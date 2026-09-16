"""Scheduler execution lanes.

The scheduler used to gate every model-backed task behind one Semaphore(1) and
let everything else run with no bound whatsoever. On 2026-09-15 three tasks came
due at 07:45 and the Skills Audit deferred three times waiting for that slot,
while a two-second "tidy sessions" pass could have run beside it a hundred times
over.

So there are three lanes now, each with its own bound. Two properties matter and
are pinned here:

1. Lightweight work does not queue behind LLM work.
2. LLM work still does not run beside other LLM work. The default model cap is
   1, and the classification is explicit membership with `model` as the
   fallback, so a task nobody has classified is slow, never parallel.
"""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from src.task_scheduler import (
    LANE_EXTERNAL,
    LANE_MAINTENANCE,
    LANE_MODEL,
    TaskScheduler,
)


@pytest.fixture
def task_db(tmp_path, monkeypatch):
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
        trigger_type = Column(String, default="schedule")
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

    engine = create_engine(f"sqlite:///{tmp_path / 'lanes.db'}")
    base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(cd, "SessionLocal", session_local)
    monkeypatch.setattr(cd, "ScheduledTask", ScheduledTask)
    monkeypatch.setattr(cd, "TaskRun", TaskRun)
    return session_local, ScheduledTask, TaskRun


def _seed(task_db, task_id, *, task_type="llm", action=None):
    session_local, ScheduledTask, _ = task_db
    db = session_local()
    try:
        db.add(ScheduledTask(
            id=task_id, owner="alice", name=task_id,
            task_type=task_type, action=action, status="active",
            trigger_type="schedule", prompt="do a thing",
        ))
        db.commit()
    finally:
        db.close()
    return task_id


def _scheduler(monkeypatch=None, **caps):
    """A scheduler built the way the other scheduler tests build one: __new__
    plus only the attributes under test. Everything lane-related is lazy for
    exactly this reason."""
    sched = TaskScheduler.__new__(TaskScheduler)
    sched._executing = set()
    sched._executing_lock = asyncio.Lock()
    sched._task_handles = {}
    if caps:
        sched._lane_caps = dict(caps)
    return sched


# ── classification ───────────────────────────────────────────────────────
@pytest.mark.parametrize("task_type,action,expected", [
    ("llm", None, LANE_MODEL),
    ("research", None, LANE_MODEL),
    ("", None, LANE_MODEL),                       # blank task_type means llm
    ("something_new", None, LANE_MODEL),          # unrecognised type
    ("action", "audit_skills", LANE_MODEL),       # model-backed action
    ("action", "summarize_emails", LANE_MODEL),
    ("action", "consolidate_memory", LANE_MODEL),
    ("action", "tidy_sessions", LANE_MAINTENANCE),
    ("action", "tidy_documents", LANE_MAINTENANCE),
    ("action", "tidy_research", LANE_MAINTENANCE),
    ("action", "daily_brief", LANE_EXTERNAL),
    ("action", "ssh_command", LANE_EXTERNAL),
    ("action", "run_script", LANE_EXTERNAL),
    ("action", "run_local", LANE_EXTERNAL),
])
def test_tasks_are_classified_by_explicit_membership(task_db, task_type, action, expected):
    sched = _scheduler()
    _seed(task_db, "t1", task_type=task_type, action=action)
    assert sched._lane_for_task("t1") == expected


@pytest.mark.parametrize("action", [
    "cookbook_serve",          # loads a model onto the same GPU the lane rations
    "some_plugin_action",      # an action this scheduler has never heard of
    "",                        # action column empty on an action task
    None,
])
def test_unclassifiable_actions_go_to_the_most_restrictive_lane(task_db, action):
    sched = _scheduler()
    _seed(task_db, "t1", task_type="action", action=action)
    assert sched._lane_for_task("t1") == LANE_MODEL


def test_a_task_we_cannot_read_goes_to_the_most_restrictive_lane(task_db):
    sched = _scheduler()
    assert sched._lane_for_task("no-such-task") == LANE_MODEL


# ── caps ─────────────────────────────────────────────────────────────────
def test_lane_caps_come_from_settings(monkeypatch):
    values = {
        "task_model_lane_concurrency": 2,
        "task_external_lane_concurrency": 5,
        "task_maintenance_lane_concurrency": 4,
    }
    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting",
                        lambda key, default=None: values.get(key, default))
    sched = _scheduler()
    assert sched._lane_cap(LANE_MODEL) == 2
    assert sched._lane_cap(LANE_EXTERNAL) == 5
    assert sched._lane_cap(LANE_MAINTENANCE) == 4


@pytest.mark.parametrize("configured,expected", [
    (0, 1),           # a lane with no slots would silently stop running work
    (-3, 1),
    (500, 8),         # background jobs on one box; do not fork-bomb the host
    ("not a number", 1),
    (None, 1),
])
def test_lane_caps_are_clamped_and_never_crash(monkeypatch, configured, expected):
    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: configured)
    sched = _scheduler()
    assert sched._lane_cap(LANE_MODEL) == expected


def test_model_lane_default_is_one(monkeypatch):
    """The pre-lane behaviour, unchanged, for an install that sets nothing."""
    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: default)
    sched = _scheduler()
    assert sched._lane_cap(LANE_MODEL) == 1


# ── gating ───────────────────────────────────────────────────────────────
class _Harness:
    """Replaces _execute_task_locked with something that parks until released,
    so lane occupancy is observable rather than a matter of timing."""

    def __init__(self, sched):
        self.sched = sched
        self.started = []
        self.gates = {}
        sched._wait_for_idle_before_slot = self._no_idle_wait
        sched._execute_task_locked = self._locked

    async def _no_idle_wait(self, task_id, run_id):
        return None

    async def _locked(self, task_id, run_id, **kw):
        self.started.append(task_id)
        await self.gates.setdefault(task_id, asyncio.Event()).wait()

    def release(self, task_id):
        self.gates.setdefault(task_id, asyncio.Event()).set()

    async def settle(self):
        # Let every ready coroutine run to its next await point.
        for _ in range(8):
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_maintenance_does_not_queue_behind_an_llm_job(task_db):
    """The bug, stated as a test: a two-second cleanup must not wait out a
    twenty-minute research run."""
    sched = _scheduler()
    _seed(task_db, "llm-job", task_type="llm")
    _seed(task_db, "tidy", task_type="action", action="tidy_sessions")
    h = _Harness(sched)

    slow = asyncio.create_task(sched._execute_task("llm-job"))
    await h.settle()
    assert h.started == ["llm-job"]

    quick = asyncio.create_task(sched._execute_task("tidy"))
    await h.settle()
    assert "tidy" in h.started, "cleanup was stuck behind the LLM job"

    h.release("tidy")
    h.release("llm-job")
    await asyncio.gather(slow, quick)


@pytest.mark.asyncio
async def test_two_model_jobs_still_serialize(task_db):
    """The property the single semaphore was protecting, kept."""
    sched = _scheduler()
    _seed(task_db, "audit", task_type="action", action="audit_skills")
    _seed(task_db, "research", task_type="research")
    h = _Harness(sched)

    first = asyncio.create_task(sched._execute_task("audit"))
    await h.settle()
    second = asyncio.create_task(sched._execute_task("research"))
    await h.settle()
    assert h.started == ["audit"], "two model jobs ran at once"

    h.release("audit")
    await h.settle()
    assert h.started == ["audit", "research"]
    h.release("research")
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_maintenance_lane_is_bounded_not_unlimited(task_db, monkeypatch):
    """Before the split, non-model tasks bypassed every slot — an unbounded
    fan-out nothing was counting."""
    import src.settings as settings
    monkeypatch.setattr(
        settings, "get_setting",
        lambda key, default=None: 2 if key == "task_maintenance_lane_concurrency" else default,
    )
    sched = _scheduler()
    for i in range(3):
        _seed(task_db, f"tidy{i}", task_type="action", action="tidy_documents")
    h = _Harness(sched)

    runs = [asyncio.create_task(sched._execute_task(f"tidy{i}")) for i in range(3)]
    await h.settle()
    assert h.started == ["tidy0", "tidy1"]

    h.release("tidy0")
    await h.settle()
    assert "tidy2" in h.started
    for i in range(3):
        h.release(f"tidy{i}")
    await asyncio.gather(*runs)


@pytest.mark.asyncio
async def test_lane_status_reports_running_and_queued_per_lane(task_db):
    """_executing used to be one undifferentiated set, which is why "three tasks
    waiting" and "an idle scheduler" looked the same from outside."""
    sched = _scheduler()
    _seed(task_db, "audit", task_type="action", action="audit_skills")
    _seed(task_db, "research", task_type="research")
    _seed(task_db, "tidy", task_type="action", action="tidy_sessions")
    h = _Harness(sched)
    # _check_due_tasks/run_task_now publish into _executing before dispatch.
    sched._executing.update({"audit", "research", "tidy"})

    runs = [asyncio.create_task(sched._execute_task(t))
            for t in ("audit", "research", "tidy")]
    await h.settle()

    status = sched.lane_status()
    assert status[LANE_MODEL]["concurrency"] == 1
    assert status[LANE_MODEL]["running"] == 1
    assert status[LANE_MODEL]["queued"] == 1
    assert sorted(status[LANE_MODEL]["task_ids"]) == ["audit", "research"]
    assert status[LANE_MAINTENANCE]["running"] == 1
    assert status[LANE_MAINTENANCE]["queued"] == 0
    assert status[LANE_MODEL]["setting"] == "task_model_lane_concurrency"

    for t in ("audit", "research", "tidy"):
        h.release(t)
    await asyncio.gather(*runs)


@pytest.mark.asyncio
async def test_lane_state_is_released_when_the_run_finishes(task_db):
    sched = _scheduler()
    _seed(task_db, "tidy", task_type="action", action="tidy_sessions")
    h = _Harness(sched)
    sched._executing.add("tidy")

    run = asyncio.create_task(sched._execute_task("tidy"))
    await h.settle()
    assert sched._executing_lanes.get("tidy") == LANE_MAINTENANCE
    h.release("tidy")
    await run
    assert "tidy" not in sched._executing_lanes
    assert sched.lane_status()[LANE_MAINTENANCE]["task_ids"] == []


@pytest.mark.asyncio
async def test_queued_run_row_names_the_lane(task_db):
    """Scheduled work is unattended: a task that is waiting has to say so, and
    say which queue it is waiting in."""
    session_local, _, TaskRun = task_db
    sched = _scheduler()
    _seed(task_db, "audit", task_type="action", action="audit_skills")
    h = _Harness(sched)

    run = asyncio.create_task(sched._execute_task("audit"))
    await h.settle()

    db = session_local()
    try:
        row = db.query(TaskRun).filter(TaskRun.task_id == "audit").first()
        assert row is not None
        assert LANE_MODEL in (row.result or "")
    finally:
        db.close()
    h.release("audit")
    await run


@pytest.mark.asyncio
async def test_queue_state_endpoint_shows_the_wait_and_scopes_task_ids(task_db, monkeypatch):
    """The Tasks UI needs a way to answer "is anything waiting, and why" without
    reading the server log — and must not leak another owner's schedule doing it."""
    import routes.task_routes as task_routes

    session_local, ScheduledTask, _ = task_db
    # routes.task_routes binds SessionLocal/ScheduledTask at import, so patching
    # core.database alone only works when this test happens to import the module
    # first. Patch the module's own names too, or the route reads the real DB.
    monkeypatch.setattr(task_routes, "SessionLocal", session_local)
    monkeypatch.setattr(task_routes, "ScheduledTask", ScheduledTask)
    # Pin who is asking: whether auth is enabled is process-global state this
    # test has no business depending on.
    monkeypatch.setattr(task_routes, "get_current_user", lambda request: "alice")
    monkeypatch.setattr(task_routes, "owner_has_admin_task_privileges", lambda user: False)
    sched = _scheduler()
    _seed(task_db, "audit", task_type="action", action="audit_skills")
    _seed(task_db, "research", task_type="research")
    db = session_local()
    try:
        db.query(ScheduledTask).filter(ScheduledTask.id == "research").update(
            {"owner": "bob"}
        )
        db.commit()
    finally:
        db.close()
    h = _Harness(sched)
    sched._executing.update({"audit", "research"})
    runs = [asyncio.create_task(sched._execute_task(t)) for t in ("audit", "research")]
    await h.settle()

    router = task_routes.setup_task_routes(sched)
    endpoint = next(
        r.endpoint for r in router.routes
        if getattr(r, "path", None) == "/api/tasks/meta/queues"
    )
    out = await endpoint(SimpleNamespace(state=SimpleNamespace(current_user="alice")))

    assert out["lanes"][LANE_MODEL]["running"] == 1
    assert out["lanes"][LANE_MODEL]["queued"] == 1
    # Both tasks occupy the lane; only alice's is named back to alice.
    assert out["lanes"][LANE_MODEL]["task_ids"] == ["audit"]

    for t in ("audit", "research"):
        h.release(t)
    await asyncio.gather(*runs)


@pytest.mark.asyncio
async def test_force_run_bypasses_every_lane(task_db):
    """"Run now, really now" is the user overriding the queue on purpose."""
    sched = _scheduler()
    _seed(task_db, "audit", task_type="action", action="audit_skills")
    _seed(task_db, "audit2", task_type="action", action="audit_skills")
    h = _Harness(sched)

    held = asyncio.create_task(sched._execute_task("audit"))
    await h.settle()
    forced = asyncio.create_task(
        sched._execute_task("audit2", bypass_model_slot=True)
    )
    await h.settle()
    assert "audit2" in h.started

    h.release("audit")
    h.release("audit2")
    await asyncio.gather(held, forced)
