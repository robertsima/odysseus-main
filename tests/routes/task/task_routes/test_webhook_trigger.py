"""POST /api/tasks/{task_id}/webhook/{token}: the token in the URL is the credential.

External callers (Zapier, n8n, curl) have no session cookie, so the auth
middleware lets this one path through (issue #621) and the handler checks
the token instead. The neighbouring task routes still need a login.
"""
import pytest

import src.database
from core.database import ScheduledTask

pytestmark = pytest.mark.security

TOKEN = "t" * 43


@pytest.fixture
def webhook_task(api, monkeypatch):
    db = src.database.SessionLocal()
    try:
        db.add(ScheduledTask(id="task-1", owner="alice", name="sync", task_type="llm", prompt="sync it",
                             trigger_type="webhook", webhook_token=TOKEN, status="active"))
        db.commit()
    finally:
        db.close()
    started = []

    async def run_task_now(task_id, *args, **kwargs):
        started.append(task_id)
        return True

    monkeypatch.setattr(api.module.task_scheduler, "run_task_now", run_task_now)
    return started


def test_the_right_token_triggers_the_task_without_a_login(api, webhook_task):
    response = api.anonymous().post(f"/api/tasks/task-1/webhook/{TOKEN}")

    assert response.status_code == 200, response.text
    assert webhook_task == ["task-1"]


def test_a_wrong_token_triggers_nothing(api, webhook_task):
    response = api.anonymous().post("/api/tasks/task-1/webhook/" + "w" * 43)

    assert response.status_code == 404
    assert webhook_task == []


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/tasks"),
    ("GET", "/api/tasks/task-1"),
    ("POST", "/api/tasks/task-1/webhook-regenerate"),
    ("POST", "/api/tasks/task-1/run"),
])
def test_the_other_task_routes_still_need_a_login(api, webhook_task, method, path):
    response = api.anonymous().request(method, path)

    assert response.status_code == 401
    assert webhook_task == []
