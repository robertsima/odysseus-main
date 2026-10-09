"""A scheduled run's tokens are added to its chat's totals.

On 2026-10-09 a 20-round scheduled task read 1.4M tokens and its chat showed 0:
the chat route and run_headless record usage, but the scheduler drives the
agent loop itself and never did.
"""
import json
from types import SimpleNamespace

import pytest

from src.task_scheduler import TaskScheduler


@pytest.fixture
def chat_row(app_db, monkeypatch):
    import routes.chat_helpers as chat_helpers
    from core.database import Session as DBSession

    monkeypatch.setattr(chat_helpers, "SessionLocal", app_db.SessionLocal)
    db = app_db.SessionLocal()
    try:
        db.add(DBSession(id="task-chat", name="Nightly", endpoint_url="http://ep/v1", model="m", owner="admin"))
        db.commit()
    finally:
        db.close()
    return app_db


def _totals(app_db):
    from core.database import Session as DBSession

    db = app_db.SessionLocal()
    try:
        row = db.query(DBSession).filter(DBSession.id == "task-chat").one()
        return row.total_input_tokens, row.total_output_tokens
    finally:
        db.close()


async def test_a_scheduled_run_adds_its_tokens_to_the_chat(chat_row, monkeypatch):
    monkeypatch.setattr("src.task_endpoint.resolve_task_candidates", lambda **kw: [])

    async def _stream(**kwargs):
        yield "data: " + json.dumps({"delta": "Prioritized 4 tasks."}) + "\n\n"
        yield "data: " + json.dumps({"type": "metrics", "data": {
            "input_tokens": 1_400_000, "output_tokens": 5_981, "model": "m"}}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", _stream)
    task = SimpleNamespace(id="t1", crew_member_id=None, endpoint_url="http://ep/v1", model="m",
                           session_id="task-chat", owner="admin", prompt="Prioritize my inbox",
                           name="Nightly", max_steps=5, character_id=None)

    await TaskScheduler(session_manager=None)._run_agent_loop("http://ep/v1", "m", task, "task-chat")

    assert _totals(chat_row) == (1_400_000, 5_981)
