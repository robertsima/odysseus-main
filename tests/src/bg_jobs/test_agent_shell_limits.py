"""A `#!bg` job is how a worker runs the whole test suite, so it gets the limits foreground bash has.

Foreground bash got a bounded worker count, an OOM score of 1000 and a process-group kill after the
2026-10-10 restarts (a test run took uvicorn, PID 1, with it). A background job spawned through its
own Popen and had none of that.
"""
import os
import sys
import time

import pytest

from src import bg_jobs

pytestmark = pytest.mark.skipif(os.name != "posix", reason="background jobs run under bash with process groups")


@pytest.fixture(autouse=True)
def jobs_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "bg_jobs.json")
    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    monkeypatch.delenv("ODYSSEUS_AGENT_SHELL", raising=False)
    return tmp_path


def _wait_done(job_id, within=15.0):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        rec = bg_jobs.get(job_id)
        if rec["status"] != "running":
            return rec
        time.sleep(0.05)
    raise AssertionError("the job did not finish")


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def _wait_gone(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _wait_for(path, within=10.0):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return int(path.read_text())
        time.sleep(0.05)
    raise AssertionError("no pid written")


def test_a_background_job_is_marked_as_an_agent_shell():
    """Without the mark `python -m tests.run full` in a `#!bg` job started one pytest worker per CPU."""
    job = bg_jobs.launch('echo "$ODYSSEUS_AGENT_SHELL $PYTEST_XDIST_AUTO_NUM_WORKERS"', "chat-1")
    rec = _wait_done(job["id"])
    assert rec["output"].strip() == "1 2"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="oom_score_adj is a Linux /proc interface")
def test_a_background_job_runs_with_the_highest_oom_score():
    """A background test run that fills memory must be the OOM killer's pick, not uvicorn."""
    job = bg_jobs.launch("cat /proc/self/oom_score_adj", "chat-1")
    assert _wait_done(job["id"])["output"].strip() == "1000"


def test_killing_a_job_ends_a_grandchild_that_ignores_sigterm(monkeypatch, tmp_path):
    """SIGTERM alone left a test worker that ignores it running after the job was stopped."""
    monkeypatch.setattr(bg_jobs, "KILL_GRACE_S", 0.3)
    pidfile = tmp_path / "pid"
    job = bg_jobs.launch(f"trap '' TERM; sleep 300 & echo $! > {pidfile}; wait", "chat-1")
    pid = _wait_for(pidfile)
    try:
        bg_jobs.kill(job["id"])
        assert _wait_gone(pid), "the grandchild survived the stop"
    finally:
        if _alive(pid):
            os.kill(pid, 9)


def test_a_job_past_its_runtime_limit_is_killed_with_its_grandchildren(monkeypatch, tmp_path):
    """The hour limit reaps a runaway suite; its workers must go with it."""
    monkeypatch.setattr(bg_jobs, "KILL_GRACE_S", 0.3)
    pidfile = tmp_path / "pid"
    job = bg_jobs.launch(f"trap '' TERM; sleep 300 & echo $! > {pidfile}; wait", "chat-1", max_runtime_s=0)
    pid = _wait_for(pidfile)
    try:
        rec = bg_jobs.get(job["id"])
        assert rec.get("timed_out")
        assert _wait_gone(pid), "the grandchild survived the timeout"
    finally:
        if _alive(pid):
            os.kill(pid, 9)
