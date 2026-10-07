"""Reading a finished job's output with manage_bg_jobs is its follow-up.

Otherwise the monitor later re-invokes the chat with the same output the agent
already acted on, and the chat answers a second time.
"""
import asyncio
import json
import time

import pytest

from src import agent_activity as act
from src import bg_jobs, constants
from src.agent_tools.bg_job_tools import ManageBgJobsTool


@pytest.fixture
def finished_job(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "bg_jobs.json")
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", tmp_path)
    act._reset_for_tests()
    (tmp_path / "job-1.log").write_text("all green", encoding="utf-8")
    (tmp_path / "job-1.exit").write_text("0", encoding="utf-8")
    bg_jobs._save({"job-1": {
        "id": "job-1", "session_id": "chat-1", "command": "npm test", "status": "running", "pid": None,
        "started_at": time.time(), "ended_at": None, "exit_code": None, "max_runtime_s": 3600,
        "followed_up": False, "log_path": str(tmp_path / "job-1.log"), "exit_path": str(tmp_path / "job-1.exit"),
    }})
    yield
    act._reset_for_tests()


def _read(job_id):
    return asyncio.run(ManageBgJobsTool().execute(json.dumps({"action": "output", "job_id": job_id}),
                                                  {"session_id": "chat-1"}))


def test_reading_a_finished_job_leaves_no_follow_up(finished_job):
    result = _read("job-1")

    assert "all green" in result["output"]
    assert bg_jobs.pending_followups() == []
