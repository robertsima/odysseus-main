"""Shared guards for web search and fetch under parallel load.

Research workflows run ~15 workers at once, each issuing web_search and
web_fetch calls. The 2026-09-27 production logs showed what that does to the
no-key providers and to fetches:

* DuckDuckGo (the ``ddgs`` metasearch client) timed out or reset 41+35 times
  in 15 minutes while its upstream backends answered 429/202, and every
  failure was followed by a direct html.duckduckgo.com request that also timed
  out. Workers spent ~25 s per call waiting on a provider that was down.
* The same URL (a dictionary page returning HTTP 403) was fetched seven times
  in three seconds because nothing remembered a failed fetch.
* Identical queries from different workers each paid for their own round
  trips.

This module holds the process-wide state that addresses those: a per-provider
concurrency gate with jittered start spacing, a per-provider circuit breaker,
a short-TTL single-flight cache for provider-chain outcomes, and a negative
cache for failed page fetches. It is stdlib-only and thread-safe; call
:func:`reset_state` to clear everything (tests do this between cases).
"""

from __future__ import annotations

import contextvars
import logging
import os
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Hashable, Iterator, Optional, Tuple
from urllib.parse import urldefrag

logger = logging.getLogger(__name__)

# Injectable clocks so tests can drive time without sleeping.
_now: Callable[[], float] = time.monotonic
_sleep: Callable[[float], None] = time.sleep


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


# ----------------------------------------------------------------------
# One deadline per search call
# ----------------------------------------------------------------------
# The agent's web_search tool gives a search 30 s. Before the deadline existed
# every layer had its own timeout and none knew about the others: SearXNG could
# be asked four times (news -> general -> no language -> defaults, 15 s each)
# plus an HTML fallback, a `site:` miss asked again, an empty chain ran again
# with a simplified query, DuckDuckGo followed, and the page fetches came
# last. The tool's asyncio.wait_for gave up at 30 s and threw everything away
# while the worker thread carried on. Now the outermost search call opens a
# SearchDeadline and every layer below asks it how long it may still take.

SEARCH_DEADLINE_ENV = "ODYSSEUS_SEARCH_DEADLINE_SECONDS"
DEFAULT_SEARCH_DEADLINE = 30.0
# Below this much time left a new upstream request is not worth starting.
MIN_ATTEMPT_SECONDS = 1.0


def search_deadline_seconds() -> float:
    """The overall budget for one search call (``ODYSSEUS_SEARCH_DEADLINE_SECONDS``)."""
    try:
        value = float(os.environ.get(SEARCH_DEADLINE_ENV, "") or DEFAULT_SEARCH_DEADLINE)
    except ValueError:
        return DEFAULT_SEARCH_DEADLINE
    return value if value > 0 else DEFAULT_SEARCH_DEADLINE


class DeadlineExceeded(TimeoutError):
    """Raised when a search step would start after the call's deadline."""


class SearchDeadline:
    """An absolute point in time (on :data:`_now`) a search must finish by."""

    def __init__(self, seconds: float):
        self.seconds = float(seconds)
        self.expires = _now() + self.seconds

    def remaining(self) -> float:
        return max(0.0, self.expires - _now())

    def expired(self, margin: float = 0.0) -> bool:
        return self.remaining() <= margin

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SearchDeadline({self.remaining():.1f}s of {self.seconds:.0f}s left)"


_current_deadline: contextvars.ContextVar[Optional[SearchDeadline]] = contextvars.ContextVar(
    "odysseus_search_deadline", default=None,
)


def current_deadline() -> Optional[SearchDeadline]:
    return _current_deadline.get()


@contextmanager
def deadline_scope(seconds: Optional[float] = None,
                   deadline: Optional[SearchDeadline] = None) -> Iterator[SearchDeadline]:
    """Run the block under a search deadline; nested scopes never extend it.

    Pass *seconds* to open a new budget or *deadline* to carry an existing one
    into another thread (context variables do not follow ``run_in_executor``
    or a bare ``ThreadPoolExecutor.submit``). An outer deadline that ends
    sooner wins.
    """
    outer = _current_deadline.get()
    if deadline is None:
        deadline = SearchDeadline(search_deadline_seconds() if seconds is None else seconds)
    if outer is not None and outer.expires < deadline.expires:
        deadline = outer
    token = _current_deadline.set(deadline)
    try:
        yield deadline
    finally:
        _current_deadline.reset(token)


def time_left(default: float = float("inf")) -> float:
    """Seconds left on the current deadline (*default* when there is none)."""
    deadline = _current_deadline.get()
    return default if deadline is None else deadline.remaining()


def out_of_time(margin: Optional[float] = None) -> bool:
    """True when the current deadline leaves less than *margin* seconds
    (default :data:`MIN_ATTEMPT_SECONDS`)."""
    deadline = _current_deadline.get()
    if deadline is None:
        return False
    return deadline.remaining() < (MIN_ATTEMPT_SECONDS if margin is None else margin)


# Extra wait past the deadline before a search stops waiting for its worker
# thread: the thread's own requests are already cut to the deadline, this only
# covers a connect + read that each used the full remaining time.
DEADLINE_JOIN_GRACE = 0.5


def run_within_deadline(fn: Callable[[], Any], deadline: SearchDeadline, on_timeout: Any,
                        what: str = "search") -> Any:
    """Return ``fn()``, or *on_timeout* once *deadline* (plus a short grace) passes.

    ``fn`` runs in a daemon thread that carries the deadline, so every step
    inside it still stops on its own; this is the backstop for a step that
    blocks longer than its timeout says (httpx applies a timeout per connect
    and per read, not to the whole request). A thread given up on finishes in
    the background and its outcome is dropped. *on_timeout* may be a callable.
    """
    outcome: Dict[str, Any] = {}
    ctx = contextvars.copy_context()

    def target() -> None:
        try:
            outcome["value"] = ctx.run(fn)
        except BaseException as exc:  # re-raised in the caller
            outcome["error"] = exc

    worker = threading.Thread(target=target, name="search-deadline", daemon=True)
    worker.start()
    worker.join(max(0.0, deadline.remaining()) + DEADLINE_JOIN_GRACE)
    if worker.is_alive():
        logger.warning(
            "%s still running at the %.0fs search time limit; returning without it",
            what, deadline.seconds,
        )
        return on_timeout() if callable(on_timeout) else on_timeout
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def request_timeout(cap: float, what: str = "search request") -> float:
    """A per-request timeout: *cap*, cut to what the deadline has left.

    Raises :class:`DeadlineExceeded` when too little time is left to start
    another request, so retries and fallbacks stop instead of running past
    the budget.
    """
    left = time_left()
    if left < MIN_ATTEMPT_SECONDS:
        raise DeadlineExceeded(f"{what}: search time limit reached")
    return max(MIN_ATTEMPT_SECONDS, min(float(cap), left))


# ----------------------------------------------------------------------
# Per-provider concurrency gate with paced, jittered starts
# ----------------------------------------------------------------------
class ProviderBusy(RuntimeError):
    """Raised when a provider slot could not be obtained in time."""


@dataclass(frozen=True)
class GateLimits:
    max_concurrent: int
    min_interval: float  # seconds between successive request starts
    jitter: float        # extra random spacing, uniform in [0, jitter]
    wait_timeout: float  # give up on a slot after this long


# SearXNG is our own container but every query fans out to its upstream
# engines, which rate-limit per source IP. ddgs fans out to ~8 public backends
# per call (google/brave answered 429 in the logs), so it gets the tightest
# budget. Keyed APIs have their own quotas and only need a concurrency bound.
_DEFAULT_LIMITS: Dict[str, GateLimits] = {
    "searxng": GateLimits(_env_int("ODYSSEUS_SEARXNG_CONCURRENCY", 4), 0.15, 0.25, 30.0),
    "duckduckgo": GateLimits(_env_int("ODYSSEUS_DDG_CONCURRENCY", 2), 0.5, 0.5, 15.0),
    # GitHub's search API allows 10 requests/minute without a token (30 with
    # one). One at a time, ~6 s apart, and a short wait: a worker that cannot
    # get a slot quickly falls through to SearXNG instead of queueing.
    "github_issues": GateLimits(1, 6.0, 0.5, 8.0),
}
_FALLBACK_LIMITS = GateLimits(4, 0.0, 0.0, 30.0)


class ProviderGate:
    """Bound concurrent calls to one provider and space out their starts."""

    def __init__(self, name: str, limits: GateLimits):
        self.name = name
        self.limits = limits
        self._sem = threading.BoundedSemaphore(limits.max_concurrent)
        self._lock = threading.Lock()
        self._next_start = 0.0

    @contextmanager
    def slot(self) -> Iterator[None]:
        # Waiting for a slot counts against the search deadline too: a worker
        # with 5 s left must not queue 30 s for SearXNG.
        left = time_left()
        wait = min(self.limits.wait_timeout, left)
        if left < MIN_ATTEMPT_SECONDS or not self._sem.acquire(timeout=wait):
            raise ProviderBusy(
                f"{self.name}: no free search slot after {wait:.0f}s "
                f"({self.limits.max_concurrent} concurrent)"
            )
        try:
            with self._lock:
                now = _now()
                start = max(now, self._next_start)
                delay = start - now
                if delay > 0 and delay > time_left() - MIN_ATTEMPT_SECONDS:
                    # The paced start would land after the deadline; do not
                    # take the start time from the next caller either.
                    raise ProviderBusy(
                        f"{self.name}: next request start is {delay:.1f}s away, past the search deadline"
                    )
                spacing = self.limits.min_interval
                if self.limits.jitter:
                    spacing += random.uniform(0, self.limits.jitter)
                self._next_start = start + spacing
            if delay > 0:
                _sleep(delay)
            yield
        finally:
            self._sem.release()


# ----------------------------------------------------------------------
# Circuit breaker
# ----------------------------------------------------------------------
class CircuitBreaker:
    """Skip a provider for a cooldown after N consecutive transport failures.

    Only transport failures (timeouts, connection resets, decode errors) count;
    an honest "no results" is neutral. After the cooldown one call is let
    through as a probe: success closes the breaker, failure re-opens it.
    Trips and recoveries each log a single INFO line.
    """

    def __init__(self, name: str, threshold: int = 3, cooldown: float = 90.0):
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0
        self._tripped = False
        self._probe_in_flight = False

    def allow(self) -> bool:
        """True when a call may go out now (closed, or the half-open probe)."""
        with self._lock:
            if not self._tripped:
                return True
            if _now() < self._open_until or self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True

    def is_open(self) -> bool:
        with self._lock:
            return self._tripped and (_now() < self._open_until or self._probe_in_flight)

    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self._open_until - _now()) if self._tripped else 0.0

    def release_probe(self) -> None:
        """End a half-open probe that produced no verdict (e.g. honest empty)."""
        with self._lock:
            self._probe_in_flight = False

    def record_success(self) -> None:
        with self._lock:
            was_tripped = self._tripped
            self._failures = 0
            self._tripped = False
            self._probe_in_flight = False
            self._open_until = 0.0
        if was_tripped:
            logger.info("%s search recovered; circuit breaker closed", self.name)

    def record_failure(self, reason: str = "") -> None:
        with self._lock:
            self._failures += 1
            self._probe_in_flight = False
            if self._failures < self.threshold:
                return
            first_trip = not self._tripped
            self._tripped = True
            self._open_until = _now() + self.cooldown
        if first_trip:
            logger.info(
                "%s search failed %d times in a row (last: %s); skipping it for %.0fs",
                self.name, self.threshold, (reason or "error")[:120], self.cooldown,
            )
        else:
            logger.debug("%s probe failed; circuit stays open for %.0fs", self.name, self.cooldown)


# ----------------------------------------------------------------------
# Named cooldowns (SearXNG engines, GitHub search)
# ----------------------------------------------------------------------
class Cooldowns:
    """Remember names that asked us to back off, each until a deadline.

    SearXNG reports rate-limited or CAPTCHA-blocked engines in
    ``unresponsive_engines``; leaving those out of the next requests keeps a
    pinned engine list from being spent on engines that cannot answer. The
    GitHub issue search parks itself here when its rate limit runs out.
    Each start of a cooldown logs one INFO line.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._until: Dict[str, Tuple[float, str]] = {}

    def cool(self, name: str, seconds: float, reason: str = "") -> None:
        if seconds <= 0:
            return
        with self._lock:
            now = _now()
            current = self._until.get(name)
            fresh = current is None or current[0] <= now
            until = now + seconds
            if current and current[0] > until:
                until = current[0]
            self._until[name] = (until, reason)
        if fresh:
            logger.info("%s: %s; leaving it out for %.0fs", name, (reason or "backing off")[:120], seconds)

    def remaining(self, name: str) -> float:
        with self._lock:
            entry = self._until.get(name)
            if entry is None:
                return 0.0
            left = entry[0] - _now()
            if left <= 0:
                self._until.pop(name, None)
                return 0.0
            return left

    def is_cooling(self, name: str) -> bool:
        return self.remaining(name) > 0

    def clear(self) -> None:
        with self._lock:
            self._until.clear()


# ----------------------------------------------------------------------
# Short-TTL single-flight cache for provider-chain outcomes
# ----------------------------------------------------------------------
RESULT_TTL_HIT = 300.0    # a result set for an identical query is reused for 5 min
RESULT_TTL_EMPTY = 60.0   # an empty outcome is reused briefly so retries don't hammer


class SingleFlightCache:
    """TTL cache where concurrent misses for one key share a single compute."""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: Dict[Hashable, Tuple[float, Any]] = {}
        self._inflight: Dict[Hashable, threading.Lock] = {}

    def _get_fresh(self, key: Hashable) -> Tuple[bool, Any]:
        entry = self._entries.get(key)
        if entry is None:
            return False, None
        expires, value = entry
        if _now() >= expires:
            self._entries.pop(key, None)
            return False, None
        return True, value

    def get_or_compute(
        self,
        key: Hashable,
        compute: Callable[[], Any],
        ttl_for: Callable[[Any], float],
    ) -> Tuple[Any, bool]:
        """Return ``(value, cache_hit)``; ``ttl_for(value) <= 0`` skips storing."""
        with self._lock:
            hit, value = self._get_fresh(key)
            if hit:
                return value, True
            key_lock = self._inflight.setdefault(key, threading.Lock())
        # Another caller computing the same key may be working to a later
        # deadline than ours; wait for it only as long as our own budget.
        wait = time_left()
        acquired = key_lock.acquire() if wait == float("inf") else key_lock.acquire(timeout=wait)
        if not acquired:
            raise DeadlineExceeded("search time limit reached while waiting for an identical search")
        try:
            with self._lock:
                hit, value = self._get_fresh(key)
                if hit:
                    return value, True
            try:
                value = compute()
                ttl = ttl_for(value)
                if ttl > 0:
                    with self._lock:
                        self._entries[key] = (_now() + ttl, value)
                        if len(self._entries) > 512:
                            now = _now()
                            for k in [k for k, (exp, _) in self._entries.items() if exp <= now]:
                                self._entries.pop(k, None)
                return value, False
            finally:
                with self._lock:
                    if self._inflight.get(key) is key_lock:
                        self._inflight.pop(key, None)
        finally:
            key_lock.release()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# ----------------------------------------------------------------------
# Negative cache for failed page fetches
# ----------------------------------------------------------------------
NEGATIVE_FETCH_TTL = 600.0
NEGATIVE_FETCH_STATUSES = frozenset({403, 404, 410, 451})
TIMEOUTS_BEFORE_CACHING = 2


class FailedFetchCache:
    """Remember URLs that failed permanently-ish so repeats return at once."""

    def __init__(self, ttl: float = NEGATIVE_FETCH_TTL):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._failed: Dict[str, Tuple[float, str]] = {}
        self._timeouts: Dict[str, Tuple[int, float]] = {}

    @staticmethod
    def _key(url: str) -> str:
        return urldefrag((url or "").strip())[0]

    def lookup(self, url: str) -> Optional[Tuple[str, float]]:
        """Return ``(error, seconds_remaining)`` for a remembered failure."""
        key = self._key(url)
        with self._lock:
            entry = self._failed.get(key)
            if entry is None:
                return None
            expires, error = entry
            remaining = expires - _now()
            if remaining <= 0:
                self._failed.pop(key, None)
                return None
            return error, remaining

    def _remember(self, key: str, error: str, why: str) -> None:
        with self._lock:
            already = key in self._failed
            self._failed[key] = (_now() + self.ttl, error)
            if len(self._failed) > 2048:
                now = _now()
                for k in [k for k, (exp, _) in self._failed.items() if exp <= now]:
                    self._failed.pop(k, None)
        if not already:
            logger.info("web fetch: %s for %s; not retrying it for %.0fs", why, key, self.ttl)

    def record_status(self, url: str, status: int, error: str) -> None:
        if status in NEGATIVE_FETCH_STATUSES:
            self._remember(self._key(url), error, f"HTTP {status}")

    def record_timeout(self, url: str, error: str) -> None:
        key = self._key(url)
        with self._lock:
            count, first = self._timeouts.get(key, (0, _now()))
            if _now() - first > self.ttl:
                count, first = 0, _now()
            count += 1
            self._timeouts[key] = (count, first)
            if len(self._timeouts) > 2048:
                self._timeouts.clear()
                self._timeouts[key] = (count, first)
        if count >= TIMEOUTS_BEFORE_CACHING:
            with self._lock:
                self._timeouts.pop(key, None)
            self._remember(key, error, f"{count} timeouts")

    def clear(self) -> None:
        with self._lock:
            self._failed.clear()
            self._timeouts.clear()


# ----------------------------------------------------------------------
# Process-wide registries
# ----------------------------------------------------------------------
_registry_lock = threading.Lock()
_gates: Dict[str, ProviderGate] = {}
_breakers: Dict[str, CircuitBreaker] = {}
search_results_cache = SingleFlightCache()
failed_fetches = FailedFetchCache()
cooldowns = Cooldowns()


def get_gate(provider: str) -> ProviderGate:
    with _registry_lock:
        gate = _gates.get(provider)
        if gate is None:
            gate = ProviderGate(provider, _DEFAULT_LIMITS.get(provider, _FALLBACK_LIMITS))
            _gates[provider] = gate
        return gate


def get_breaker(provider: str) -> CircuitBreaker:
    with _registry_lock:
        breaker = _breakers.get(provider)
        if breaker is None:
            breaker = CircuitBreaker(provider)
            _breakers[provider] = breaker
        return breaker


def reset_state() -> None:
    """Forget gates, breakers and caches (tests; also safe at runtime)."""
    with _registry_lock:
        _gates.clear()
        _breakers.clear()
    search_results_cache.clear()
    failed_fetches.clear()
    cooldowns.clear()
