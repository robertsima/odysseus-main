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


# ── wrap up: a soft stop delivered as a steer ────────────────────────────────


@pytest.fixture
def activity_dir(tmp_path, monkeypatch):
    from src import constants

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    wb.activity._reset_for_tests()
    yield tmp_path
    wb.activity._reset_for_tests()


def test_wrap_up_unknown_run_is_404(client, activity_dir):
    assert client.post("/api/workbench/runs/nope/wrap-up").status_code == 404


def test_wrap_up_finished_run_is_409(client, activity_dir):
    rid = wb.activity.run_started("w-done", "session", "Worker: done")
    wb.activity.run_finished("w-done", "session", rid, "Worker finished")
    assert client.post(f"/api/workbench/runs/{rid}/wrap-up").status_code == 409


def test_wrap_up_run_without_agent_rounds_is_409(client, activity_dir):
    # Live, but nothing drains a steer for it (a background shell job).
    rid = wb.activity.run_started("w-job", "bg_job", "sleep 600")
    assert client.post(f"/api/workbench/runs/{rid}/wrap-up").status_code == 409


def test_wrap_up_queues_a_steer_bound_to_the_worker_run(client, activity_dir, monkeypatch):
    from src import agent_control, headless_agent

    rid = wb.activity.run_started("w-live", "session", "Worker: long task")
    # run_headless registers its wrapper run as the session's steering target.
    monkeypatch.setitem(headless_agent._STEER_RUNS, "w-live", {rid})
    try:
        res = client.post(f"/api/workbench/runs/{rid}/wrap-up")
        assert res.status_code == 200 and res.json()["queued"] is True
        queued = agent_control.pending_steer("w-live", run_id=rid)
        assert [r["text"] for r in queued] == [agent_control.WRAP_UP_TEXT]
        assert queued[0]["owner"] == "alice" and queued[0]["run_id"] == rid
    finally:
        agent_control.clear_steer("w-live", run_id=rid)


def test_wrap_up_a_chat_turn_goes_to_the_queue_its_stream_drains(client, activity_dir, monkeypatch):
    from src import agent_control

    rid = wb.activity.run_started("chat-live", "odysseus", "Turn")
    # A foreground turn drains the queue its stream allocated, not the
    # activity run id; `steer` resolves that itself.
    monkeypatch.setattr(agent_control, "is_steerable", lambda sid: True)
    monkeypatch.setattr(agent_control, "_live_run_id", lambda sid: "steer-abc")
    try:
        assert client.post(f"/api/workbench/runs/{rid}/wrap-up").status_code == 200
        assert [r["text"] for r in agent_control.pending_steer("chat-live", run_id="steer-abc")] == [
            agent_control.WRAP_UP_TEXT]
    finally:
        agent_control.clear_steer("chat-live", run_id="steer-abc")
