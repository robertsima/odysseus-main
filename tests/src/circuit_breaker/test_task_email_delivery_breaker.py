"""Email delivery for task output, guarded by a circuit breaker.

Delivery is the *last* thing a run does, so an SMTP host that has stopped
resolving converts successful task runs into failed ones — and each failure
costs a 30-second connect timeout inside the run slot. The Sept 15-16 audit
lists exactly that ("email network/DNS outages").

The risk of guarding it is the opposite mistake: a rejected recipient or a
stale password is the server *answering*, and treating that as an outage would
stop a working mail account from being tried for the next half hour while the
underlying problem — a typo in the task's output target — went unfixed. So the
two cases are asserted side by side.
"""
import smtplib
from types import SimpleNamespace

import pytest

from src.task_scheduler import TaskScheduler


@pytest.fixture(autouse=True)
def _fresh_breakers():
    from src.circuit_breaker import reset_all
    reset_all()
    yield
    reset_all()


@pytest.fixture
def smtp(monkeypatch):
    """Stub the two email helpers the scheduler reaches for, and count sends."""
    import routes.email_helpers as helpers
    import routes.email_routes as routes

    state = SimpleNamespace(sends=0, raises=None)

    def _resolve_send_config(account_id=None, owner=""):
        return {
            "smtp_host": "smtp.example.com",
            "smtp_port": 465,
            "smtp_user": "me@example.com",
            "from_address": "me@example.com",
        }

    def _send(cfg, from_addr, recipients, message, timeout=30):
        state.sends += 1
        if state.raises is not None:
            raise state.raises

    monkeypatch.setattr(routes, "_resolve_send_config", _resolve_send_config, raising=False)
    monkeypatch.setattr(helpers, "_send_smtp_message", _send, raising=False)
    return state


def _task():
    return SimpleNamespace(id="t1", name="Daily Digest", owner="alice@example.com")


def _scheduler():
    return TaskScheduler.__new__(TaskScheduler)


@pytest.mark.asyncio
async def test_a_dead_smtp_host_stops_being_dialled(smtp):
    from src.circuit_breaker import CircuitOpen

    sched = _scheduler()
    smtp.raises = smtplib.SMTPConnectError(421, "cannot connect")

    for _ in range(3):
        with pytest.raises(smtplib.SMTPConnectError):
            await sched._deliver_via_email("email:self", _task(), "body")
    assert smtp.sends == 3

    with pytest.raises(CircuitOpen):
        await sched._deliver_via_email("email:self", _task(), "body")
    assert smtp.sends == 3, "the fourth delivery should not have touched SMTP"


@pytest.mark.asyncio
async def test_a_dns_failure_counts_as_an_outage(smtp):
    import socket

    from src.circuit_breaker import CircuitOpen

    sched = _scheduler()
    smtp.raises = socket.gaierror(-3, "Temporary failure in name resolution")

    for _ in range(3):
        with pytest.raises(socket.gaierror):
            await sched._deliver_via_email("email:self", _task(), "body")
    with pytest.raises(CircuitOpen):
        await sched._deliver_via_email("email:self", _task(), "body")


@pytest.mark.asyncio
async def test_rejected_recipients_never_take_the_account_offline(smtp):
    """The server answered. Keep trying — and keep surfacing the real error,
    which is the one the user has to fix."""
    sched = _scheduler()
    smtp.raises = smtplib.SMTPRecipientsRefused({"nope@example.com": (550, b"no such user")})

    for _ in range(8):
        with pytest.raises(smtplib.SMTPRecipientsRefused):
            await sched._deliver_via_email("email:nope@example.com", _task(), "body")
    assert smtp.sends == 8


@pytest.mark.asyncio
async def test_bad_credentials_never_take_the_account_offline(smtp):
    sched = _scheduler()
    smtp.raises = smtplib.SMTPAuthenticationError(535, "bad credentials")

    for _ in range(8):
        with pytest.raises(smtplib.SMTPAuthenticationError):
            await sched._deliver_via_email("email:self", _task(), "body")
    assert smtp.sends == 8


@pytest.mark.asyncio
async def test_a_delivery_that_works_keeps_the_circuit_closed(smtp):
    sched = _scheduler()
    smtp.raises = smtplib.SMTPConnectError(421, "cannot connect")
    for _ in range(2):
        with pytest.raises(smtplib.SMTPConnectError):
            await sched._deliver_via_email("email:self", _task(), "body")

    smtp.raises = None
    await sched._deliver_via_email("email:self", _task(), "body")

    smtp.raises = smtplib.SMTPConnectError(421, "cannot connect")
    for _ in range(2):
        with pytest.raises(smtplib.SMTPConnectError):
            await sched._deliver_via_email("email:self", _task(), "body")
    # Two failures since the success: under the threshold, still delivering.
    smtp.raises = None
    await sched._deliver_via_email("email:self", _task(), "body")
    assert smtp.sends == 6


@pytest.mark.asyncio
async def test_the_open_circuit_says_why_and_for_how_long(smtp):
    """Unattended work: the run row and the log have to carry the reason, or a
    task that stopped delivering looks like a task that stopped running."""
    from src.circuit_breaker import CircuitOpen

    sched = _scheduler()
    smtp.raises = smtplib.SMTPServerDisconnected("connection closed")
    for _ in range(3):
        with pytest.raises(smtplib.SMTPServerDisconnected):
            await sched._deliver_via_email("email:self", _task(), "body")

    with pytest.raises(CircuitOpen) as exc:
        await sched._deliver_via_email("email:self", _task(), "body")
    text = str(exc.value)
    assert "smtp.example.com" in text
    assert "cooldown active" in text
    assert exc.value.retry_after > 0
