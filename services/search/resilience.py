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
        if not self._sem.acquire(timeout=self.limits.wait_timeout):
            raise ProviderBusy(
                f"{self.name}: no free search slot after {self.limits.wait_timeout:.0f}s "
                f"({self.limits.max_concurrent} concurrent)"
            )
        try:
            with self._lock:
                now = _now()
                start = max(now, self._next_start)
                spacing = self.limits.min_interval
                if self.limits.jitter:
                    spacing += random.uniform(0, self.limits.jitter)
                self._next_start = start + spacing
            delay = start - now
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
        with key_lock:
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
