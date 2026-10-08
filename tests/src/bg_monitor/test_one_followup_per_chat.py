"""Jobs of one chat that finish together are followed up in one turn.

2026-10-07: three test runs a worker had started ended within seconds of each
other after its turn. Each started its own follow-up turn, re-sent the whole
chat, and wrote the worker's hand-back to the parent again.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from src import agent_activity as act
from src import bg_jobs, bg_monitor, constants


class _Manager:
    def __init__(self, sess):
        self.sess = sess
        self.added = []

    def get_session(self, sid):
        return self.sess if sid == self.sess.id else None

    def add_message(self, sid, msg):
        self.added.append(msg)

    def save_sessions(self):
        pass


@pytest.fixture
def chat(tmp_path, monkeypatch):
    from src import agent_control, ai_interaction

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "bg_jobs.json")
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", tmp_path)
    act._reset_for_tests()
    sess = SimpleNamespace(id="chat-1", owner="alice", model="m", get_context_messages=lambda: [])
    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: _Manager(sess))

    async def no_hand_up(*args, **kwargs):
        return None

    monkeypatch.setattr(agent_control, "_hand_up_when_done", no_hand_up)
    jobs = {}
    for job_id, output in (("job-unit", "300 passed"), ("job-browser", "12 passed"), ("job-lint", "clean")):
        (tmp_path / f"{job_id}.log").write_text(output, encoding="utf-8")
        (tmp_path / f"{job_id}.exit").write_text("0", encoding="utf-8")
        jobs[job_id] = {
            "id": job_id, "session_id": "chat-1", "command": job_id, "status": "running", "pid": None,
            "started_at": time.time(), "ended_at": None, "exit_code": None, "max_runtime_s": 3600,
            "followed_up": False, "log_path": str(tmp_path / f"{job_id}.log"),
            "exit_path": str(tmp_path / f"{job_id}.exit"),
        }
    bg_jobs._save(jobs)
    yield sess
    act._reset_for_tests()


def test_jobs_that_finish_together_share_one_follow_up_turn(chat, monkeypatch):
    turns = []

    async def fake_drain(sess, messages, *, run_id=None):
        turns.append("\n".join(str(m.get("content")) for m in messages))
        return "All three runs passed.", []

    monkeypatch.setattr(bg_monitor, "_drain_agent", fake_drain)

    first = bg_jobs.pending_followups()[0]
    assert asyncio.run(bg_monitor._run_followup(first)) is True
    bg_jobs.mark_followed_up(first["id"])

    assert len(turns) == 1
    assert all(text in turns[0] for text in ("300 passed", "12 passed", "clean"))
    assert bg_jobs.pending_followups() == []


def test_a_failed_follow_up_leaves_the_other_jobs_for_the_retry(chat, monkeypatch):
    async def failing_drain(sess, messages, *, run_id=None):
        raise RuntimeError("upstream 500")

    monkeypatch.setattr(bg_monitor, "_drain_agent", failing_drain)

    first = bg_jobs.pending_followups()[0]
    with pytest.raises(RuntimeError):
        asyncio.run(bg_monitor._run_followup(first))

    assert len(bg_jobs.pending_followups()) == 3
