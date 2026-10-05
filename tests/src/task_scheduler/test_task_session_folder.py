"""A task that has no session yet gets one in the Tasks folder.

The sidebar groups scheduled output under Tasks. A session created without
the folder lands in the user's main chat list next to their conversations.
"""
from types import SimpleNamespace

import pytest

import core.database
import src.deep_research
import src.research_handler
from src.task_scheduler import TaskScheduler


@pytest.fixture
def scheduler(app_db, monkeypatch, tmp_path):
    monkeypatch.setattr(core.database, "SessionLocal", app_db.SessionLocal)
    monkeypatch.setattr(src.research_handler, "RESEARCH_DATA_DIR", tmp_path / "research")
    return TaskScheduler(session_manager=None)


def _task(**fields):
    task = dict(
        id="t1", owner="alice", name="nightly digest", prompt="Summarize my day",
        max_steps=1, endpoint_url="http://ep.test/v1", model="m", crew_member_id=None,
        character_id=None, session_id=None, task_type="llm", output_target="session",
    )
    return SimpleNamespace(**{**task, **fields})


def _sessions(app_db):
    db = app_db.SessionLocal()
    try:
        return db.query(core.database.Session).all()
    finally:
        db.close()


async def test_llm_task_session_is_in_the_tasks_folder(scheduler, app_db):
    async def fake_run(*args, **kwargs):
        return "done"

    scheduler._run_agent_loop = fake_run

    await scheduler._execute_llm_task(_task(), None)

    [session] = _sessions(app_db)
    assert (session.folder, session.owner) == ("Tasks", "alice")


async def test_delivered_action_result_session_is_in_the_tasks_folder(scheduler, app_db):
    await scheduler._deliver_task_result(_task(task_type="action"), "all quiet", None)

    [session] = _sessions(app_db)
    assert (session.folder, session.owner) == ("Tasks", "alice")


async def test_research_task_session_is_in_the_tasks_folder(scheduler, app_db, monkeypatch):
    class FakeResearcher:
        findings = []

        def __init__(self, **kwargs):
            pass

        async def research(self, prompt):
            return "report"

        def get_stats(self):
            return {}

    monkeypatch.setattr(src.deep_research, "DeepResearcher", FakeResearcher)

    await scheduler._execute_research_task(_task(task_type="research"), None)

    [session] = _sessions(app_db)
    assert (session.folder, session.owner) == ("Tasks", "alice")
