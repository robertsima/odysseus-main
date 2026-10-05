"""Circuit breaker behaviour, and the line between "down" and "bad request".

The dangerous failure mode for a breaker is not failing to trip — that just
leaves the old behaviour. It is tripping on an answer: a 400, a refused
recipient, a rejected password. Those mean the dependency is up and *we* are
wrong, and cutting the integration off for ten minutes because of one malformed
call is strictly worse than making the call. Most of this file is about that
line; the rest pins the open/half-open/close cycle.

The clock is injected everywhere, so nothing here sleeps or depends on
wall-clock timing.
"""
import smtplib
import socket

import pytest
from fastapi import HTTPException

from src.circuit_breaker import (
    STATE_CLOSED,
    STATE_HALF_OPEN,
    STATE_OPEN,
    CircuitBreaker,
    CircuitOpen,
    get_breaker,
    is_dependency_down,
    is_smtp_dependency_down,
)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _breaker(**kw):
    clock = _Clock()
    kw.setdefault("failure_threshold", 3)
    kw.setdefault("cooldown_seconds", 60.0)
    kw.setdefault("max_cooldown_seconds", 240.0)
    return CircuitBreaker("test", clock=clock, **kw), clock


# ── open / half-open / close ─────────────────────────────────────────────
def test_closed_until_the_threshold_is_reached():
    br, _ = _breaker()
    for _ in range(2):
        assert br.allow("host") == STATE_CLOSED
        br.record_failure("host", error="502 overload")
    # Two in a row is not yet a pattern.
    assert br.allow("host") == STATE_CLOSED
    assert br.state("host") == STATE_CLOSED


def test_third_consecutive_failure_opens_and_fails_fast():
    br, _ = _breaker()
    for _ in range(3):
        br.record_failure("host", error="502 overload")
    assert br.state("host") == STATE_OPEN
    with pytest.raises(CircuitOpen) as exc:
        br.allow("host")
    assert exc.value.failures == 3
    assert exc.value.retry_after == pytest.approx(60.0)


def test_success_resets_the_streak():
    br, _ = _breaker()
    br.record_failure("host")
    br.record_failure("host")
    br.record_success("host")
    br.record_failure("host")
    br.record_failure("host")
    # Only two since the success, so still closed.
    assert br.allow("host") == STATE_CLOSED


def test_one_probe_is_admitted_when_the_cooldown_expires():
    br, clock = _breaker()
    for _ in range(3):
        br.record_failure("host")
    clock.advance(61)
    assert br.allow("host") == STATE_HALF_OPEN
    # The probe is exclusive: a second caller must not stampede a dependency
    # that has had one request in the last minute.
    with pytest.raises(CircuitOpen):
        br.allow("host")


def test_successful_probe_closes_the_circuit():
    br, clock = _breaker()
    for _ in range(3):
        br.record_failure("host")
    clock.advance(61)
    br.allow("host")
    br.record_success("host")
    assert br.state("host") == STATE_CLOSED
    assert br.allow("host") == STATE_CLOSED


def test_failed_probe_reopens_with_a_doubled_cooldown():
    br, clock = _breaker()
    for _ in range(3):
        br.record_failure("host")
    clock.advance(61)
    br.allow("host")
    br.record_failure("host", error="still down")
    with pytest.raises(CircuitOpen) as exc:
        br.allow("host")
    assert exc.value.retry_after == pytest.approx(120.0)


def test_cooldown_growth_is_capped():
    br, clock = _breaker()
    for _ in range(3):
        br.record_failure("host")
    for _ in range(6):
        clock.advance(10_000)
        br.allow("host")
        br.record_failure("host")
    with pytest.raises(CircuitOpen) as exc:
        br.allow("host")
    assert exc.value.retry_after == pytest.approx(240.0)


def test_keys_are_independent():
    br, _ = _breaker()
    for _ in range(3):
        br.record_failure("dead-host")
    with pytest.raises(CircuitOpen):
        br.allow("dead-host")
    assert br.allow("healthy-host") == STATE_CLOSED


# ── "bad request" must never trip anything ───────────────────────────────
def test_request_level_failures_never_open_the_circuit():
    br, _ = _breaker()
    for _ in range(25):
        br.record_failure("host", dependency_down=False, error="400 invalid_request")
    assert br.allow("host") == STATE_CLOSED
    assert br.snapshot() == {}


def test_a_request_level_failure_clears_a_pending_streak():
    br, _ = _breaker()
    br.record_failure("host")
    br.record_failure("host")
    # The dependency answered — it is demonstrably reachable.
    br.record_failure("host", dependency_down=False, error="400")
    br.record_failure("host")
    assert br.allow("host") == STATE_CLOSED


# ── classification ───────────────────────────────────────────────────────
@pytest.mark.parametrize("err", [
    HTTPException(502, "upstream overloaded"),
    HTTPException(503, "service unavailable"),
    HTTPException(504, "gateway timeout"),
    HTTPException(429, "rate limited"),
    TimeoutError("read timed out"),
    ConnectionError("connection reset by peer"),
    RuntimeError("Cannot reach https://chatgpt.com"),
])
def test_recognized_as_dependency_down(err):
    assert is_dependency_down(err)


@pytest.mark.parametrize("err", [
    HTTPException(400, "model 'gpt-5.4' does not exist"),
    HTTPException(401, "Invalid API key"),
    HTTPException(403, "forbidden"),
    HTTPException(404, "no such endpoint"),
    HTTPException(500, "model crashed on this prompt"),
    ValueError("prompt is empty"),
    KeyError("choices"),
])
def test_not_treated_as_dependency_down(err):
    assert not is_dependency_down(err)


def test_our_own_fast_fail_is_not_evidence_of_anything():
    # Otherwise a CircuitOpen raised by one breaker, caught and reported to
    # another, would keep a circuit open on the strength of its own output.
    assert not is_dependency_down(CircuitOpen("x", "k", 5.0, 3))


def test_circuit_open_reads_as_transient_to_the_scheduler():
    """The scheduler reschedules on transient upstream errors. A run skipped by
    the breaker must land in that bucket, not in "this task is broken"."""
    from src.llm_core import is_transient_upstream_error

    err = CircuitOpen("scheduled_model_endpoint", "https://api.example", 300.0, 3)
    assert "cooldown active" in str(err)
    assert is_transient_upstream_error(err)


@pytest.mark.parametrize("err", [
    smtplib.SMTPConnectError(421, "cannot connect"),
    smtplib.SMTPServerDisconnected("connection closed"),
    socket.gaierror(-3, "Temporary failure in name resolution"),
    TimeoutError("timed out"),
    OSError(111, "Connection refused"),
])
def test_smtp_failures_that_mean_the_host_is_gone(err):
    assert is_smtp_dependency_down(err)


@pytest.mark.parametrize("err", [
    smtplib.SMTPAuthenticationError(535, "bad credentials"),
    smtplib.SMTPRecipientsRefused({"nobody@example.com": (550, b"no such user")}),
    smtplib.SMTPSenderRefused(553, b"sender rejected", "me@example.com"),
    smtplib.SMTPDataError(554, b"message rejected"),
    RuntimeError("Reconnect this Google account to send mail"),
])
def test_smtp_answers_are_not_outages(err):
    """Every one of these is the server replying. Tripping on them would take a
    working mail account offline because one message was wrong — the exact
    mistake src/oauth_errors.py documents."""
    assert not is_smtp_dependency_down(err)


# ── observability ────────────────────────────────────────────────────────
def test_snapshot_is_empty_when_healthy_and_detailed_when_not():
    br, _ = _breaker()
    assert br.snapshot() == {}
    for _ in range(3):
        br.record_failure("host", error="502 overload")
    snap = br.snapshot()["host"]
    assert snap["state"] == STATE_OPEN
    assert snap["consecutive_failures"] == 3
    assert snap["trips"] == 1
    assert "502 overload" in snap["last_error"]
    assert snap["retry_in_seconds"] > 0


def test_registry_returns_the_same_instance():
    a = get_breaker("shared-under-test", failure_threshold=2)
    b = get_breaker("shared-under-test")
    assert a is b
    a.reset()
