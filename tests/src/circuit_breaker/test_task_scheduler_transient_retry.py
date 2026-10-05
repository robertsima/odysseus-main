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


# ── circuit breaker, and how it composes with the retry above ────────────
#
# The retry budget answers "run *this task* again later". It does nothing about
# the next task pointed at the same dead endpoint, which pays the same timeout
# from scratch — that is the "repeated upstream 502 overload" pattern in the
# Sept 15-16 logs. The breaker is the memory between tasks, and the two have to
# agree: a run the breaker skips must be rescheduled, not written off.


@pytest.fixture(autouse=True)
def _fresh_breakers():
    from src.circuit_breaker import reset_all
    reset_all()
    yield
    reset_all()


def _endpoint_calls(sched, url, err=None, result="ok"):
    """Drive `_guarded_model_call`, reporting how often the endpoint was
    actually dialled — which is the whole point of the breaker."""
    calls = {"n": 0}

    async def _call():
        calls["n"] += 1
        if err is not None:
            raise err
        return result

    async def _drive():
        return await sched._guarded_model_call(url, _call)

    return calls, _drive


@pytest.mark.asyncio
async def test_repeated_overload_stops_being_dialled():
    from src.circuit_breaker import CircuitOpen

    sched = _scheduler()
    url = "https://api.example.com/v1/chat/completions"
    calls, drive = _endpoint_calls(sched, url, err=HTTPException(502, "upstream overloaded"))

    for _ in range(3):
        with pytest.raises(HTTPException):
            await drive()
    assert calls["n"] == 3

    # The fourth attempt never reaches the endpoint.
    with pytest.raises(CircuitOpen):
        await drive()
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_a_short_circuited_run_is_rescheduled_not_written_off():
    """CircuitOpen has to land in the transient bucket, or the breaker would
    turn a recoverable outage into a task that lost its slot for good."""
    from src.circuit_breaker import CircuitOpen

    sched = _scheduler()
    url = "https://api.example.com/v1/chat/completions"
    _, drive = _endpoint_calls(sched, url, err=HTTPException(502, "overloaded"))
    for _ in range(3):
        with pytest.raises(HTTPException):
            await drive()
    with pytest.raises(CircuitOpen) as exc:
        await drive()

    assert sched._transient_retry_delay("t1", exc.value) is not None


@pytest.mark.asyncio
async def test_a_bad_request_never_takes_the_endpoint_offline():
    """A 400 is the provider answering. Cutting a working endpoint off for five
    minutes because one task's model name was wrong would be a worse bug than
    the one the breaker fixes."""
    sched = _scheduler()
    url = "https://api.example.com/v1/chat/completions"
    calls, drive = _endpoint_calls(
        sched, url, err=HTTPException(400, "model 'gpt-5.4' does not exist")
    )
    for _ in range(10):
        with pytest.raises(HTTPException):
            await drive()
    assert calls["n"] == 10


@pytest.mark.asyncio
async def test_a_success_clears_the_streak():
    sched = _scheduler()
    url = "https://api.example.com/v1/chat/completions"
    _, fail = _endpoint_calls(sched, url, err=HTTPException(502, "overloaded"))
    _, ok = _endpoint_calls(sched, url)
    for _ in range(2):
        with pytest.raises(HTTPException):
            await fail()
    assert await ok() == "ok"
    for _ in range(2):
        with pytest.raises(HTTPException):
            await fail()
    # Two failures since the success — under the threshold, still dialling.
    assert await ok() == "ok"


@pytest.mark.asyncio
async def test_endpoints_are_judged_separately():
    from src.circuit_breaker import CircuitOpen

    sched = _scheduler()
    dead = "https://dead.example.com/v1/chat/completions"
    live = "https://live.example.com/v1/chat/completions"
    _, fail = _endpoint_calls(sched, dead, err=HTTPException(502, "overloaded"))
    for _ in range(3):
        with pytest.raises(HTTPException):
            await fail()
    with pytest.raises(CircuitOpen):
        await fail()

    _, ok = _endpoint_calls(sched, live)
    assert await ok() == "ok"


@pytest.mark.asyncio
async def test_a_cancelled_run_says_nothing_about_the_endpoint():
    """Foreground activity cancels background runs constantly. Counting those
    as endpoint failures would trip the breaker on a healthy provider."""
    import asyncio

    from src.circuit_breaker import get_breaker

    sched = _scheduler()
    url = "https://api.example.com/v1/chat/completions"
    _, cancelled = _endpoint_calls(sched, url, err=asyncio.CancelledError())
    for _ in range(5):
        with pytest.raises(asyncio.CancelledError):
            await cancelled()
    assert get_breaker("scheduled_model_endpoint").snapshot() == {}
