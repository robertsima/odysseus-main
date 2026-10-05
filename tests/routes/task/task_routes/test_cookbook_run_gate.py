"""Running a Cookbook serve task by hand needs an admin.

Serving a model starts a process on a host the owner may not control, so a
task saved with that action cannot be launched by its non-admin owner.
"""
import pytest

import src.database
from core.database import ScheduledTask

pytestmark = pytest.mark.security


@pytest.fixture
def started(api, monkeypatch):
    runs = []

    async def run_task_now(task_id, *args, **kwargs):
        runs.append(task_id)
        return True

    monkeypatch.setattr(api.module.task_scheduler, "run_task_now", run_task_now)
    return runs


def _task(task_id, owner, action):
    db = src.database.SessionLocal()
    try:
        db.add(ScheduledTask(
            id=task_id, owner=owner, name=task_id, prompt="{}", task_type="action", action=action,
            trigger_type="webhook", status="active", output_target="session",
        ))
        db.commit()
    finally:
        db.close()


def test_a_non_admin_cannot_manually_run_their_cookbook_serve_task(api, started):
    _task("alice-task", "alice", "cookbook_serve")

    response = api.as_user("alice").post("/api/tasks/alice-task/run")

    assert response.status_code == 403
    assert started == []


def test_a_non_admin_can_run_their_other_action_tasks(api, started):
    _task("alice-mail", "alice", "summarize_emails")

    response = api.as_user("alice").post("/api/tasks/alice-mail/run")

    assert response.status_code == 200, response.text
    assert started == ["alice-mail"]


def test_an_admin_can_run_their_cookbook_serve_task(api, started):
    _task("admin-task", "admin", "cookbook_serve")

    response = api.as_admin().post("/api/tasks/admin-task/run")

    assert response.status_code == 200, response.text
    assert started == ["admin-task"]
