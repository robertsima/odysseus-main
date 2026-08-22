"""Regression tests for retrying a task run after a transient upstream outage.

Background: a scheduled task whose LLM call failed because the container
briefly lost DNS ("Cannot reach https://chatgpt.com") was written off as an
error and `next_run` advanced to the *next occurrence* — so a daily "Important
Email Triage" lost the entire day to a few unreachable seconds.

Transient-looking failures now get a bounded short retry instead. The
classifier has to stay conservative: a bad prompt, an auth failure or a model
error must still fail on the spot, or a permanently broken task would retry
forever.
"""
import socket

import httpx
import pytest
from fastapi import HTTPException

from src.llm_core import is_transient_upstream_error
from src.task_scheduler import TaskScheduler


@pytest.mark.parametrize("err", [
    HTTPException(503, "Cannot reach https://chatgpt.com"),
    RuntimeError("Cannot reach https://chatgpt.com (HTTP 503)"),
    HTTPException(503, "Upstream https://chatgpt.com marked unreachable (cooldown active)"),
    httpx.ConnectError("[Errno -3] Temporary failure in name resolution"),
    httpx.ConnectTimeout("timed out"),
    httpx.ReadTimeout("timed out"),
    socket.gaierror(-3, "Temporary failure in name resolution"),
    OSError("[Errno 111] Connection refused"),
])
def test_transient_failures_are_recognized(err):
    assert is_transient_upstream_error(err)


@pytest.mark.parametrize("err", [
    HTTPException(401, "Invalid API key"),
    HTTPException(400, "model 'gpt-5.4' does not exist"),
    HTTPException(429, "Rate limit exceeded"),
    ValueError("prompt is empty"),
    KeyError("choices"),
])
def test_real_failures_are_not_treated_as_transient(err):
    assert not is_transient_upstream_error(err)


def _scheduler():
    # Built with __new__ like the other scheduler tests: the retry counter is
    # created on first use, so no hand-initialized state is needed.
    return TaskScheduler.__new__(TaskScheduler)


def test_transient_failure_backs_off_then_gives_up():
    sched = _scheduler()
    err = HTTPException(503, "Cannot reach https://chatgpt.com")
    delays = [sched._transient_retry_delay("t1", err) for _ in range(4)]
    assert delays[:3] == list(TaskScheduler._TRANSIENT_RETRY_DELAYS)
    # Bounded: the fourth failure takes the normal "record the error and move
    # to the next occurrence" path instead of retrying forever.
    assert delays[3] is None


def test_non_transient_failure_never_retries_and_clears_the_counter():
    sched = _scheduler()
    assert sched._transient_retry_delay("t1", HTTPException(503, "Cannot reach x")) is not None
    assert sched._task_transient_retries["t1"] == 1
    assert sched._transient_retry_delay("t1", HTTPException(401, "Invalid API key")) is None
    assert "t1" not in sched._task_transient_retries


def test_manual_runs_surface_the_error_instead_of_deferring():
    # The user triggered it and is waiting on the answer.
    sched = _scheduler()
    err = HTTPException(503, "Cannot reach https://chatgpt.com")
    assert sched._transient_retry_delay("t1", err, manual=True) is None
    # ...and it doesn't quietly eat part of the scheduled run's retry budget.
    assert sched._transient_retry_counts() == {}


def test_retry_budget_is_per_task():
    sched = _scheduler()
    err = HTTPException(503, "Cannot reach https://chatgpt.com")
    for _ in range(3):
        sched._transient_retry_delay("t1", err)
    assert sched._transient_retry_delay("t1", err) is None
    # A second task's budget is untouched by the first one exhausting its own.
    assert sched._transient_retry_delay("t2", err) == TaskScheduler._TRANSIENT_RETRY_DELAYS[0]
