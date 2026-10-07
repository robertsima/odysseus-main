"""A background job that finishes while its chat's turn runs is read in that turn.

2026-10-07: a worker started its test suites with `#!bg` and kept working. The
jobs finished mid-turn, the monitor deferred them until the turn ended, and
then each one started a follow-up turn that re-sent the worker's 100k-token
chat and wrote the worker's hand-back again (three hand-backs for one task).
"""
import asyncio
import json
import time

import pytest

import src.agent_loop as al
from src import agent_activity as act
from src import bg_jobs, constants


@pytest.fixture
def job_store(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "bg_jobs.json")
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", tmp_path)
    act._reset_for_tests()
    yield tmp_path
    act._reset_for_tests()


def _finish_job(tmp_path, job_id, session_id, output):
    (tmp_path / f"{job_id}.log").write_text(output, encoding="utf-8")
    (tmp_path / f"{job_id}.exit").write_text("0", encoding="utf-8")
    jobs = bg_jobs._load()
    jobs[job_id] = {
        "id": job_id, "session_id": session_id, "command": "python -m pytest", "status": "running",
        "pid": None, "started_at": time.time(), "ended_at": None, "exit_code": None,
        "max_runtime_s": 3600, "followed_up": False,
        "log_path": str(tmp_path / f"{job_id}.log"), "exit_path": str(tmp_path / f"{job_id}.exit"),
    }
    bg_jobs._save(jobs)


def test_a_job_that_finishes_mid_turn_reaches_the_next_round(job_store, monkeypatch):
    seen = []

    async def fake_model(_candidates, messages, **kwargs):
        seen.append([dict(m) for m in messages])
        if len(seen) == 1:
            # The suite the agent started earlier finishes during this round.
            _finish_job(job_store, "job-suite", "chat-1", "412 passed in 61s")
            call = {"id": "call_1", "name": "read_file", "arguments": json.dumps({"path": "src/a.py"})}
            yield f'data: {json.dumps({"type": "tool_calls", "calls": [call]})}\n\n'
        else:
            yield f'data: {json.dumps({"delta": "Suite passed."})}\n\n'
        yield "data: [DONE]\n\n"

    async def fake_tool(block, *args, **kwargs):
        return block.tool_type, {"output": "print('a')", "exit_code": 0}

    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_model)
    monkeypatch.setattr(al, "execute_tool_block", fake_tool)
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None)

    async def run():
        loop = al.stream_agent_loop(
            "http://model.test/v1", "m", [{"role": "user", "content": "run the suite and fix failures"}],
            relevant_tools={"read_file"}, session_id="chat-1",
        )
        return [chunk async for chunk in loop]

    asyncio.run(run())

    assert len(seen) == 2
    assert any("412 passed in 61s" in str(m.get("content")) for m in seen[1])
    # The monitor has nothing left to start a second turn for.
    assert bg_jobs.pending_followups() == []


def test_another_chats_job_is_left_for_its_own_chat(job_store):
    from src.bg_monitor import deliver_to_live_turn

    _finish_job(job_store, "job-other", "chat-2", "done")

    assert deliver_to_live_turn("chat-1") == []
    assert [rec["id"] for rec in bg_jobs.pending_followups()] == ["job-other"]
