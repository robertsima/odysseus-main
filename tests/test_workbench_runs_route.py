"""The runs endpoint says, in the server log, what it told the agent strip.

"The strip shows nothing" reports could not be read from the server side at
all: the poll is throttled and silent in the browser, and the route logged
nothing. One line per change (not per poll) is enough to tell whether the
browser is asking for a chat's runs and how many it was given.
"""
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.workbench_routes as wb


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(wb, "require_user", lambda request: "alice")
    monkeypatch.setattr(wb, "require_admin", lambda request: None)
    wb._RUNS_ANSWERED.clear()
    app = FastAPI()
    app.include_router(wb.setup_workbench_routes())
    return TestClient(app)


def test_runs_logs_once_per_change_not_per_poll(client, monkeypatch, caplog):
    rows = [{"run_id": "session-1", "status": "running"}]
    monkeypatch.setattr(wb.activity, "list_runs", lambda **kw: list(rows))
    with caplog.at_level(logging.INFO, logger=wb.__name__):
        for _ in range(3):
            assert client.get("/api/workbench/runs?session_id=chat-1").json()["runs"] == rows
        rows[0]["status"] = "completed"
        client.get("/api/workbench/runs?session_id=chat-1")
        # A different chat is its own line.
        client.get("/api/workbench/runs?session_id=chat-2")
    lines = [r.getMessage() for r in caplog.records if "[workbench] runs for chat" in r.getMessage()]
    assert lines == [
        "[workbench] runs for chat chat-1: 1 row(s), 1 running",
        "[workbench] runs for chat chat-1: 1 row(s), 0 running",
        "[workbench] runs for chat chat-2: 1 row(s), 0 running",
    ]


def test_runs_without_a_session_filter_is_not_logged(client, monkeypatch, caplog):
    monkeypatch.setattr(wb.activity, "list_runs", lambda **kw: [])
    with caplog.at_level(logging.INFO, logger=wb.__name__):
        client.get("/api/workbench/runs")
    assert not [r for r in caplog.records if "[workbench] runs" in r.getMessage()]
