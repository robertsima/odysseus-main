"""Single-flight + short-TTL answers for the UI's polling endpoints.

The browser polls a handful of read-only endpoints every few seconds per tab
(the agent strip's ``/api/workbench/runs``, the sidebar's ``/api/chat/runs``,
the Agents dashboard's ``/api/agents/overview`` ...). Each answer is built
from in-memory registries guarded by *threading* locks and from SQLite reads,
so building it on the event loop can block the loop behind whichever worker
thread holds that lock or the SQLite write lock -- and when the loop is slow,
the polls pile up and every one of them repeats the same work.

:func:`coalesced` builds an answer once, in a worker thread, for everyone
asking the same question at the same time (single-flight), and serves that
answer to repeat askers for ``ttl`` seconds. Callers must treat the returned
value as read-only: it is shared between requests.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, Hashable, Tuple

_MAX_ENTRIES = 512

_cache: Dict[Hashable, Tuple[float, Any]] = {}
_inflight: Dict[Hashable, Tuple[asyncio.AbstractEventLoop, "asyncio.Task[Any]"]] = {}


def _prune(now: float) -> None:
    if len(_cache) <= _MAX_ENTRIES:
        return
    for key in [k for k, (exp, _) in _cache.items() if exp <= now]:
        _cache.pop(key, None)
    if len(_cache) > _MAX_ENTRIES:
        _cache.clear()


def _retrieve_exception(task: "asyncio.Task[Any]") -> None:
    # Every waiter may have gone (client disconnects); don't let the loop log
    # "Task exception was never retrieved" for a poll nobody is waiting on.
    if not task.cancelled():
        task.exception()


async def _compute(key: Hashable, fn: Callable[[], Any], ttl: float) -> Any:
    try:
        value = await asyncio.to_thread(fn)
        if ttl > 0:
            now = time.monotonic()
            _cache[key] = (now + ttl, value)
            _prune(now)
        return value
    finally:
        entry = _inflight.get(key)
        if entry is not None and entry[1] is asyncio.current_task():
            _inflight.pop(key, None)


async def coalesced(key: Hashable, fn: Callable[[], Any], *, ttl: float = 1.0) -> Any:
    """``fn()`` run off the event loop, shared by concurrent identical asks.

    ``key`` must capture everything the answer depends on (the caller's
    identity and every query parameter). ``ttl`` <= 0 disables the reuse
    window but keeps the single-flight. A caller that is cancelled (the
    browser gave up) does not cancel the shared build for the others.
    """
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    loop = asyncio.get_running_loop()
    entry = _inflight.get(key)
    if entry is None or entry[0] is not loop or entry[1].done():
        task = loop.create_task(_compute(key, fn, ttl))
        task.add_done_callback(_retrieve_exception)
        _inflight[key] = (loop, task)
    else:
        task = entry[1]
    return await asyncio.shield(task)


def invalidate(prefix: Any = None) -> None:
    """Forget cached answers (all, or those whose key tuple starts with ``prefix``)."""
    if prefix is None:
        _cache.clear()
        return
    for key in list(_cache):
        if isinstance(key, tuple) and key and key[0] == prefix:
            _cache.pop(key, None)


def _reset_for_tests() -> None:
    _cache.clear()
    _inflight.clear()
