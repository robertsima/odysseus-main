"""Event-loop lag detector: say when the loop stalls, and what was holding it.

Odysseus is one process with one asyncio loop. Every UI poll, every agent
stream and every worker share it, so one piece of synchronous work on the loop
(a SQLite commit, a big ``json.dump``, a regex over a long transcript) stalls
all of them at once. From the server log that looked like "every endpoint got
slow together" -- ``slow_request`` lines for handlers that only read a dict,
all finishing in the same millisecond -- with nothing naming the cause.

Two cheap probes, started once from the app lifespan:

* a coroutine that sleeps ``interval`` and measures how late it woke up
  (the classic drift probe), aggregating the worst lag per report window;
* a watchdog thread that notices when that coroutine has not ticked for
  ``threshold`` seconds and, *while the loop is still stuck*, records the
  loop thread's current stack -- application frames, led by the innermost
  library frame (``lib:<package path>``) when the loop is inside a library
  call, as ``file:line:function`` (no locals, no arguments, no SQL). A stall
  with no application frame names its library frames; one the watchdog could
  not sample (GIL held throughout) says so, or names the GC pass that caused it.

A stall logs one WARNING, rate-limited::

    [loop-lag] event loop blocked 7.42s (stalls=3 since last report, worst=7.42s)
        blocked_in=context_compactor.py:598:apply_compaction_state_for_session <- agent_loop.py:7344:...

Knobs: ``ODYSSEUS_LOOP_LAG_WARN_SECONDS`` (default 1.0, 0 disables),
``ODYSSEUS_LOOP_LAG_REPORT_INTERVAL`` (min seconds between WARNINGs, default 30).
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("app.loop_lag")

# normcase: on Windows resolve() and co_filename can disagree on letter case.
_APP_ROOT = os.path.normcase(str(Path(__file__).resolve().parent.parent))
_SELF = os.path.normcase(str(Path(__file__).resolve()))


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _is_app_file(norm: str) -> bool:
    return norm.startswith(_APP_ROOT) and norm != _SELF and "site-packages" not in norm


def _lib_label(filename: str) -> str:
    """Short, recognisable name for a library frame's file:
    ``sqlalchemy/orm/session.py`` for an installed package, ``asyncio/events.py``
    for the stdlib."""
    parts = Path(filename).parts
    for marker in ("site-packages", "dist-packages"):
        if marker in parts:
            return "/".join(parts[parts.index(marker) + 1:])
    return "/".join(parts[-2:])


def app_stack(frame, limit: int = 6) -> List[str]:
    """``file:line:function`` for the innermost application frames of a stack.

    The useful answer is which of *our* call sites was on the loop, so library
    frames are skipped -- except the innermost one, kept first as
    ``lib:<package path>:line:function`` when the loop was stuck inside a
    library call. A stall entirely inside library code (an anyio/mcp task, an
    asyncio callback: no project frame on the stack at all) is reported by its
    innermost library frames instead of an empty list, which the log used to
    print as ``blocked_in=unknown``. Innermost first.
    """
    out: List[str] = []
    lib: List[str] = []
    depth = 0
    while frame is not None and depth < 80 and len(out) < limit:
        filename = frame.f_code.co_filename
        norm = os.path.normcase(filename)
        if _is_app_file(norm):
            out.append(f"{Path(filename).name}:{frame.f_lineno}:{frame.f_code.co_name}")
        elif norm != _SELF and not out and len(lib) < 3:
            lib.append(f"lib:{_lib_label(filename)}:{frame.f_lineno}:{frame.f_code.co_name}")
        frame = frame.f_back
        depth += 1
    if not out:
        return lib
    if lib:
        out = [lib[0], *out[:max(1, limit - 1)]]
    return out


class LoopLagMonitor:
    def __init__(self, *, threshold: Optional[float] = None, interval: float = 0.25,
                 report_interval: Optional[float] = None):
        self.threshold = _float_env("ODYSSEUS_LOOP_LAG_WARN_SECONDS", 1.0) if threshold is None else threshold
        self.interval = interval
        self.report_interval = (_float_env("ODYSSEUS_LOOP_LAG_REPORT_INTERVAL", 30.0)
                                if report_interval is None else report_interval)
        self._lock = threading.Lock()
        self._last_tick = time.monotonic()
        self._loop_thread_id: Optional[int] = None
        self._stop = threading.Event()
        self._task: Optional[asyncio.Task] = None
        self._thread: Optional[threading.Thread] = None
        # Watchdog's view of the current stall (captured while it is happening).
        self._stall_reported = False
        self._stall_stack: List[str] = []
        # Aggregates.
        self.max_lag = 0.0
        self.stalls = 0
        self.total_stalled = 0.0
        self.last_stall: Optional[Dict[str, Any]] = None
        self._window_stalls = 0
        self._window_worst = 0.0
        self._window_stack: List[str] = []
        self._last_report = 0.0
        # Garbage-collection time since the ticker last woke. A collection
        # holds the GIL, so the watchdog cannot sample the loop while it runs;
        # timing it is the only way to name that kind of stall.
        self._gc_started: Optional[float] = None
        self._gc_since_tick = 0.0
        self._gc_worst: Optional[Dict[str, Any]] = None

    # ── probes ───────────────────────────────────────────────────────────────

    async def _ticker(self) -> None:
        self._loop_thread_id = threading.get_ident()
        # The first wake-up also measures the time since start(): startup work
        # that blocks the loop before this task ever ran is a stall too.
        with self._lock:
            since_start = time.monotonic() - self._last_tick
        self._end_tick(since_start)
        while not self._stop.is_set():
            start = time.monotonic()
            with self._lock:
                self._last_tick = start
            await asyncio.sleep(self.interval)
            self._end_tick(time.monotonic() - start - self.interval)

    def _end_tick(self, lag: float) -> None:
        with self._lock:
            self._last_tick = time.monotonic()
            stack = self._stall_stack
            self._stall_stack = []
            self._stall_reported = False
            gc_s, gc_info = self._gc_since_tick, self._gc_worst
            self._gc_since_tick, self._gc_worst = 0.0, None
        if lag < self.threshold:
            return
        if gc_info is not None and gc_s >= lag * 0.5:
            stack = [f"gc(generation={gc_info['generation']}, {gc_s:.2f}s)", *stack]
        elif not stack:
            # The watchdog never got to sample while the loop was stuck: the
            # loop thread held the GIL the whole time (a C extension that does
            # not release it) and resumed first.
            stack = ["unsampled(the loop thread held the GIL; a C call or GC that does not release it)"]
        self._record(lag, stack)

    def _gc_callback(self, phase: str, info: Dict[str, Any]) -> None:
        if phase == "start":
            self._gc_started = time.monotonic()
            return
        started, self._gc_started = self._gc_started, None
        if started is None:
            return
        took = time.monotonic() - started
        with self._lock:
            self._gc_since_tick += took
            if self._gc_worst is None or took >= self._gc_worst["seconds"]:
                self._gc_worst = {"seconds": took, "generation": info.get("generation")}

    def _watchdog(self) -> None:
        poll = max(0.05, min(self.threshold / 4.0, 0.5))
        while not self._stop.wait(poll):
            with self._lock:
                stalled_for = time.monotonic() - self._last_tick - self.interval
                already = self._stall_reported
            if stalled_for < self.threshold or already or self._loop_thread_id is None:
                continue
            frame = sys._current_frames().get(self._loop_thread_id)
            try:
                stack = app_stack(frame) if frame is not None else []
            finally:
                del frame
            with self._lock:
                self._stall_reported = True
                self._stall_stack = stack

    def _record(self, lag: float, stack: List[str]) -> None:
        now = time.monotonic()
        self.stalls += 1
        self.total_stalled += lag
        self.max_lag = max(self.max_lag, lag)
        self.last_stall = {"seconds": round(lag, 3), "at": time.time(), "blocked_in": stack}
        self._window_stalls += 1
        if lag >= self._window_worst:
            self._window_worst = lag
            self._window_stack = stack or self._window_stack
        if now - self._last_report < self.report_interval:
            return
        self._last_report = now
        logger.warning(
            "[loop-lag] event loop blocked %.2fs (stalls=%d since last report, worst=%.2fs) blocked_in=%s",
            lag, self._window_stalls, self._window_worst,
            " <- ".join(self._window_stack) or "unknown",
        )
        self._window_stalls = 0
        self._window_worst = 0.0
        self._window_stack = []

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Start both probes on the running loop. No-op when disabled."""
        if self.threshold <= 0 or self._task is not None:
            return False
        self._stop.clear()
        # Known before the first tick: startup work that blocks the loop right
        # after start() (before the ticker ever ran) is sampled too.
        self._loop_thread_id = threading.get_ident()
        with self._lock:
            self._last_tick = time.monotonic()
        if self._gc_callback not in gc.callbacks:
            gc.callbacks.append(self._gc_callback)
        self._task = asyncio.get_running_loop().create_task(self._ticker(), name="loop-lag-probe")
        self._thread = threading.Thread(target=self._watchdog, name="loop-lag-watchdog", daemon=True)
        self._thread.start()
        return True

    async def stop(self) -> None:
        self._stop.set()
        try:
            gc.callbacks.remove(self._gc_callback)
        except ValueError:
            pass
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def snapshot(self) -> Dict[str, Any]:
        return {
            "threshold_s": self.threshold,
            "stalls": self.stalls,
            "max_lag_s": round(self.max_lag, 3),
            "total_stalled_s": round(self.total_stalled, 3),
            "last_stall": self.last_stall,
        }


_monitor: Optional[LoopLagMonitor] = None


def start_monitor() -> Optional[LoopLagMonitor]:
    """Start the process-wide monitor once (call from the running loop)."""
    global _monitor
    if _monitor is None:
        _monitor = LoopLagMonitor()
    try:
        _monitor.start()
    except Exception:
        logger.debug("loop lag monitor failed to start", exc_info=True)
    return _monitor


async def stop_monitor() -> None:
    if _monitor is not None:
        await _monitor.stop()


def snapshot() -> Dict[str, Any]:
    return _monitor.snapshot() if _monitor is not None else {"stalls": 0, "enabled": False}
