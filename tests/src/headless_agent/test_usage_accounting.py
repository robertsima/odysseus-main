"""A headless run's tokens are added to its chat's totals.

On 2026-10-07 a worker read 28M input tokens over 291 rounds and its chat
showed 0: only the chat route called accumulate_token_usage, and workers,
background-job follow-ups and continuations all run through run_headless.
"""
import asyncio
import json

import pytest

from src import agent_activity as act
from src import constants


@pytest.fixture
def chat_row(app_db, tmp_path, monkeypatch):
    import routes.chat_helpers as chat_helpers
    from core.database import Session as DBSession

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(chat_helpers, "SessionLocal", app_db.SessionLocal)
    act._reset_for_tests()
    db = app_db.SessionLocal()
    try:
        db.add(DBSession(id="w-usage", name="worker", endpoint_url="http://llm.test/v1", model="m", owner="alice"))
        db.commit()
    finally:
        db.close()
    yield app_db
    act._reset_for_tests()


def _frame(payload):
    return f"data: {json.dumps(payload)}\n\n"


def _totals(app_db):
    from core.database import Session as DBSession

    db = app_db.SessionLocal()
    try:
        row = db.query(DBSession).filter(DBSession.id == "w-usage").one()
        return row.total_input_tokens, row.total_output_tokens
    finally:
        db.close()


def _run(monkeypatch, frames):
    import src.agent_loop as agent_loop
    import src.model_context as model_context
    from core.models import Session
    from src.headless_agent import run_headless

    async def fake_loop(url, model, messages, **kwargs):
        for frame in frames:
            yield _frame(frame)
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop, raising=False)
    monkeypatch.setattr(model_context, "get_context_length", lambda url, model: 32_000)
    sess = Session(id="w-usage", name="worker", endpoint_url="http://llm.test/v1", model="m", owner="alice")
    rid = act.run_started(sess.id, "session", "Worker")
    asyncio.run(run_headless(sess, [{"role": "user", "content": "go"}], activity_session_id=sess.id, run_id=rid))


def test_a_finished_worker_run_adds_its_tokens_to_the_chat(chat_row, monkeypatch):
    _run(monkeypatch, [
        {"type": "round_usage", "round": 1, "input": 10_000, "cached": 0, "output": 200},
        {"delta": "Done."},
        {"type": "metrics", "data": {"input_tokens": 10_000, "output_tokens": 200, "model": "m"}},
    ])

    assert _totals(chat_row) == (10_000, 200)


def test_a_run_without_final_metrics_still_counts_its_rounds(chat_row, monkeypatch):
    _run(monkeypatch, [
        {"type": "round_usage", "round": 1, "input": 7_000, "cached": 6_000, "output": 50},
        {"type": "agent_step", "round": 2},
        {"type": "round_usage", "round": 2, "input": 8_000, "cached": 7_000, "output": 60},
    ])

    assert _totals(chat_row) == (15_000, 110)
