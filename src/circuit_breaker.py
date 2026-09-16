"""Circuit breakers for external dependencies that fail in streaks, not one-offs.

The Sept 15-16 production logs are full of the same shape: an upstream answers
``502 overload`` (or the SMTP host's DNS stops resolving), and every scheduled
run that touches it pays the full timeout again, and again, and again. Each
attempt is individually reasonable — the previous one *might* have been a blip —
but collectively they burn the run slot, the agent's rounds and the user's
patience on a dependency that is plainly not coming back this minute.

A breaker is the missing memory between attempts. After N consecutive failures
of a given dependency we stop dialling for a cooldown and fail immediately with
a reason the caller can log and show; when the cooldown expires exactly one
probe is let through (half-open) and its outcome decides whether the circuit
closes or the cooldown doubles.

Two things this module is deliberately careful about:

**It does not trip on our own bad requests.** A 400, a rejected recipient, a
bad API key — those are answers. The dependency is up; *we* are wrong. Tripping
on them would take a working integration offline because one call was
malformed, which is the exact failure mode :mod:`src.oauth_errors` was written
to avoid ("call it terminal when it was a blip and the UI nags about
reconnecting a working account"). So callers classify every failure the same
way that module does — terminal/request-level vs transient/dependency-level —
and only the transient ones move the counter. See :func:`is_dependency_down`.

**An open circuit is never silent.** Scheduled tasks run unattended, so a
breaker that quietly skips work is worse than the outage it is protecting
against. Opening, probing and closing are all logged at WARNING/INFO, the
:class:`CircuitOpen` message names the dependency and the remaining cooldown,
and :meth:`CircuitBreaker.snapshot` exposes the whole registry for the UI.

The ``CircuitOpen`` message is also worded to contain "cooldown active", which
is one of :data:`src.llm_core._TRANSIENT_UPSTREAM_MARKERS`. That is not
cosmetic: it makes ``is_transient_upstream_error(CircuitOpen(...))`` true, so
the task scheduler's existing transient-retry backoff treats a short-circuited
run as "try again in a few minutes" rather than "this task is broken".
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict

logger = logging.getLogger(__name__)

STATE_CLOSED = "closed"        # dialling normally
STATE_OPEN = "open"            # cooling down; every call fails fast
STATE_HALF_OPEN = "half_open"  # cooldown expired; one probe is in flight


class CircuitOpen(RuntimeError):
    """Raised instead of attempting a dependency whose circuit is open.

    Carries the structured bits (`name`, `key`, `retry_after`, `failures`) so a
    caller can render its own message without parsing this one, and a message
    that already reads well in a log line or a Tasks Activity row.
    """

    def __init__(self, name: str, key: str, retry_after: float, failures: int,
                 last_error: str = ""):
        self.name = name
        self.key = key
        self.retry_after = max(0.0, float(retry_after))
        self.failures = int(failures)
        self.last_error = last_error or ""
        where = f"{name}[{key}]" if key else name
        tail = f" — last error: {self.last_error[:200]}" if self.last_error else ""
        super().__init__(
            f"{where} unreachable — circuit breaker cooldown active after "
            f"{self.failures} consecutive failures; retrying in "
            f"{self.retry_after:.0f}s{tail}"
        )


class _KeyState:
    """Per-dependency counters. One instance per breaker key."""

    __slots__ = ("failures", "open_until", "cooldown", "probing", "last_error",
                 "opened_count")

    def __init__(self):
        self.failures = 0
        self.open_until = 0.0
        self.cooldown = 0.0      # the cooldown currently in force, for doubling
        self.probing = False     # a half-open probe has been handed out
        self.last_error = ""
        self.opened_count = 0    # lifetime trips, for the UI/diagnostics


class CircuitBreaker:
    """Consecutive-failure breaker, sharded by an arbitrary dependency key.

    One breaker instance represents one *kind* of dependency (the scheduler's
    model endpoint, the SMTP transport); `key` separates instances of it (a
    host, an account id), so one dead endpoint never silences a healthy one.

    Guarded by a plain ``threading.Lock`` rather than an asyncio lock: like
    :mod:`src.llm_core`'s dead-host map, this is read from the event loop *and*
    from sync code running in FastAPI's threadpool, and the critical sections
    are a handful of integer operations.
    """

    def __init__(self, name: str, *, failure_threshold: int = 3,
                 cooldown_seconds: float = 120.0,
                 max_cooldown_seconds: float = 900.0,
                 clock: Callable[[], float] = time.monotonic):
        self.name = name
        # Three, not one: a single 502 from a load balancer mid-deploy is noise.
        # Three in a row with no success between them is a pattern.
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(1.0, float(cooldown_seconds))
        self.max_cooldown_seconds = max(self.cooldown_seconds,
                                        float(max_cooldown_seconds))
        self._clock = clock
        self._states: Dict[str, _KeyState] = {}
        self._lock = threading.Lock()

    # ── internals ────────────────────────────────────────────────────────
    def _state_for(self, key: str) -> _KeyState:
        st = self._states.get(key)
        if st is None:
            st = _KeyState()
            self._states[key] = st
        return st

    # ── the gate ─────────────────────────────────────────────────────────
    def allow(self, key: str = "") -> str:
        """Gate one attempt. Returns the state the attempt runs under.

        Raises :class:`CircuitOpen` when the circuit is open and the cooldown
        has not expired. When it *has* expired, exactly one caller is let
        through as the half-open probe and the rest keep failing fast until
        that probe reports back — otherwise a burst of due tasks would all
        stampede a dependency that has had no chance to recover.
        """
        with self._lock:
            st = self._state_for(key)
            if st.open_until <= 0.0:
                return STATE_CLOSED
            now = self._clock()
            if now < st.open_until:
                raise CircuitOpen(self.name, key, st.open_until - now,
                                  st.failures, st.last_error)
            if st.probing:
                # Someone else already holds the probe. Report the cooldown as
                # nominally elapsed rather than negative.
                raise CircuitOpen(self.name, key, 0.0, st.failures, st.last_error)
            st.probing = True
            logger.info(
                "Circuit breaker %s[%s] half-open: letting one probe through "
                "after %d consecutive failures", self.name, key, st.failures,
            )
            return STATE_HALF_OPEN

    def state(self, key: str = "") -> str:
        """Current state without consuming a probe (for UI and logging)."""
        with self._lock:
            st = self._states.get(key)
            if st is None or st.open_until <= 0.0:
                return STATE_CLOSED
            if st.probing:
                return STATE_HALF_OPEN
            return STATE_OPEN if self._clock() < st.open_until else STATE_HALF_OPEN

    # ── outcomes ─────────────────────────────────────────────────────────
    def record_success(self, key: str = "") -> None:
        """The dependency answered. Close the circuit and forget the streak."""
        with self._lock:
            st = self._states.get(key)
            if st is None:
                return
            was_open = st.open_until > 0.0
            st.failures = 0
            st.open_until = 0.0
            st.cooldown = 0.0
            st.probing = False
            st.last_error = ""
        if was_open:
            logger.info("Circuit breaker %s[%s] closed — dependency recovered",
                        self.name, key)

    def record_failure(self, key: str = "", *, dependency_down: bool = True,
                       error: str = "") -> bool:
        """Record one failed attempt. Returns True if this opened the circuit.

        ``dependency_down=False`` means the dependency answered and rejected
        *this request* (a 400, a bad recipient, a revoked key). That is not
        evidence of an outage — it is evidence the dependency is alive and
        talking — so it clears the streak instead of advancing it. Mirrors the
        ``TOKEN_TERMINAL`` / ``TOKEN_TRANSIENT`` split in
        :mod:`src.oauth_errors`: the two answers need opposite handling and
        collapsing them is what makes a breaker dangerous.
        """
        with self._lock:
            st = self._state_for(key)
            if not dependency_down:
                # Alive-and-rejecting resets the streak *and* an open circuit:
                # we have positive proof the dependency is reachable, and the
                # caller's own error path is the right place for a bad request.
                st.failures = 0
                st.open_until = 0.0
                st.cooldown = 0.0
                st.probing = False
                return False
            st.last_error = (error or "")[:500]
            was_probing = st.probing
            st.probing = False
            st.failures += 1
            if not was_probing and st.failures < self.failure_threshold:
                return False
            # Either the streak reached the threshold, or the half-open probe
            # failed. Both mean "still down" — back off, doubling each time so
            # a long outage costs a handful of probes rather than one per tick.
            st.cooldown = (min(st.cooldown * 2, self.max_cooldown_seconds)
                           if st.cooldown else self.cooldown_seconds)
            st.open_until = self._clock() + st.cooldown
            st.opened_count += 1
            cooldown, failures = st.cooldown, st.failures
        logger.warning(
            "Circuit breaker %s[%s] OPEN for %.0fs after %d consecutive "
            "failures: %s", self.name, key, cooldown, failures,
            (error or "no detail")[:200],
        )
        return True

    def reset(self, key: str | None = None) -> None:
        """Clear state for one key, or all of them. Used by tests and by an
        explicit operator "try again now"."""
        with self._lock:
            if key is None:
                self._states.clear()
            else:
                self._states.pop(key, None)

    def snapshot(self) -> dict:
        """Serialisable view of every key this breaker knows about.

        Only keys with something to report are included, so a healthy system
        renders as an empty dict rather than a wall of zeroes.
        """
        now = self._clock()
        out: dict[str, dict] = {}
        with self._lock:
            for key, st in self._states.items():
                if st.failures == 0 and st.open_until <= 0.0:
                    continue
                open_now = st.open_until > now and not st.probing
                out[key] = {
                    "state": (STATE_OPEN if open_now
                              else STATE_HALF_OPEN if st.open_until > 0.0
                              else STATE_CLOSED),
                    "consecutive_failures": st.failures,
                    "retry_in_seconds": max(0.0, round(st.open_until - now, 1)),
                    "trips": st.opened_count,
                    "last_error": st.last_error,
                }
        return out


# ── registry ─────────────────────────────────────────────────────────────
# Breakers have to outlive the call that uses them — that memory is the whole
# point — so call sites fetch a shared instance by name instead of building one
# per request. Construction parameters are honoured on first use only; later
# callers get the existing breaker, so one owner defines the policy.
_registry: Dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def get_breaker(name: str, **kwargs) -> CircuitBreaker:
    with _registry_lock:
        br = _registry.get(name)
        if br is None:
            br = CircuitBreaker(name, **kwargs)
            _registry[name] = br
        return br


def all_snapshots() -> dict:
    """Per-breaker snapshots for diagnostics endpoints. Empty when healthy."""
    with _registry_lock:
        breakers = list(_registry.items())
    return {name: snap for name, br in breakers if (snap := br.snapshot())}


def reset_all() -> None:
    """Drop every breaker's state. Tests only — never call this from app code,
    it would erase an in-progress cooldown for every dependency at once."""
    with _registry_lock:
        breakers = list(_registry.values())
    for br in breakers:
        br.reset()


# ── failure classification ───────────────────────────────────────────────
# Connection-level exception types that mean "we never got an answer". Kept as
# a tuple of *types* so the check is cheap and cannot be fooled by an error
# message that merely mentions a timeout.
_DOWN_EXC_TYPES: tuple[type, ...] = (TimeoutError, ConnectionError)

# Statuses that mean the dependency itself is refusing work rather than
# objecting to this request. 502/503/504 are the overload responses from the
# logs. 429 is included even though ``is_transient_upstream_error`` excludes it:
# that function answers "should this task be rescheduled", where a rate limit is
# a genuine failure of the run, while a breaker answers "is it worth dialling at
# all", and hammering a rate-limited dependency is precisely what we must stop
# (the Firecrawl entries in the same audit). 408 is a server-side timeout.
_DOWN_STATUSES = frozenset({408, 429, 502, 503, 504})


def is_dependency_down(exc: BaseException) -> bool:
    """Whether ``exc`` is evidence the dependency is out, not that we asked badly.

    Conservative on purpose: anything this does not recognise is treated as a
    request-level fault and will *not* advance a breaker toward tripping. A
    breaker that fails open is a missed optimisation; one that fails closed on
    a misread exception takes a working integration offline.
    """
    if isinstance(exc, CircuitOpen):
        # Our own fast-fail. It was never an attempt, so it is no evidence
        # either way. Call sites should not record it at all — this is the
        # backstop for the ones that catch broadly and forget.
        return False
    if isinstance(exc, _DOWN_EXC_TYPES):
        return True
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(exc, "status", None)
    if isinstance(status, int) and status in _DOWN_STATUSES:
        return True
    if isinstance(status, int) and status:
        # The dependency answered with some other status (401, 400, 404, 500…).
        # It is up; this request or this configuration is the problem.
        return False
    try:
        # Lazy: llm_core pulls in httpx and the whole model stack, and this
        # module is imported by the scheduler at definition time.
        from src.llm_core import is_transient_upstream_error
        return bool(is_transient_upstream_error(exc))
    except Exception:
        return False


# SMTP failures do not carry HTTP statuses, and smtplib's exception hierarchy
# already draws the line we need: the connection-level errors below mean the
# host or the network is gone, while SMTPAuthenticationError,
# SMTPRecipientsRefused, SMTPSenderRefused and SMTPDataError are the server
# telling us this particular message or credential is wrong.
_SMTP_DOWN_NAMES = frozenset({
    "SMTPConnectError",        # could not open the connection at all
    "SMTPServerDisconnected",  # connection dropped mid-conversation
    "SMTPHeloError",           # host answered but not as a working SMTP server
    "SMTPNotSupportedError",   # STARTTLS/AUTH unavailable — usually a broken relay
})


def is_smtp_dependency_down(exc: BaseException) -> bool:
    """SMTP-flavoured :func:`is_dependency_down`.

    Split out rather than folded in because the generic classifier would read
    ``SMTPAuthenticationError`` (a dead credential — terminal, exactly the case
    :mod:`src.oauth_errors` exists for) as unclassified and therefore
    non-tripping by luck rather than by decision. Here it is a decision.
    """
    try:
        import smtplib
        if isinstance(exc, smtplib.SMTPException):
            # Note smtplib.SMTPException subclasses OSError, so this branch has
            # to come first or the raw-socket rule below would swallow refused
            # recipients and bad credentials as "the host is down".
            return type(exc).__name__ in _SMTP_DOWN_NAMES
    except Exception:  # pragma: no cover - smtplib is stdlib, but stay safe
        pass
    # DNS failure, refused connect, TLS handshake timeout: these arrive as bare
    # socket/OS errors from inside smtplib and are the "email network/DNS
    # outage" entries in the audit.
    if isinstance(exc, OSError):
        return True
    return is_dependency_down(exc)
