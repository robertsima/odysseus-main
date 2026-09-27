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
  loop thread's current stack -- application frames only, as
  ``file:line:function`` (no locals, no arguments, no SQL).

A stall logs one WARNING, rate-limited::

    [loop-lag] event loop blocked 7.42s (stalls=3 since last report, worst=7.42s)
        blocked_in=context_compactor.py:598:apply_compaction_state_for_session <- agent_loop.py:7344:...

Knobs: ``ODYSSEUS_LOOP_LAG_WARN_SECONDS`` (default 1.0, 0 disables),
``ODYSSEUS_LOOP_LAG_REPORT_INTERVAL`` (min seconds between WARNINGs, default 30).
"""

from __future__ import annotations

import asyncio
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


def app_stack(frame, limit: int = 6) -> List[str]:
    """``file:line:function`` for the innermost application frames of a stack.

    Library frames (sqlite3, sqlalchemy, json) are skipped: the useful answer
    is which of *our* call sites was on the loop. Innermost first.
    """
    out: List[str] = []
    depth = 0
    while frame is not None and depth < 80 and len(out) < limit:
        filename = frame.f_code.co_filename
        norm = os.path.normcase(filename)
        if norm.startswith(_APP_ROOT) and norm != _SELF and "site-packages" not in norm:
            out.append(f"{Path(filename).name}:{frame.f_lineno}:{frame.f_code.co_name}")
        frame = frame.f_back
        depth += 1
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

    # ── probes ───────────────────────────────────────────────────────────────

    async def _ticker(self) -> None:
        self._loop_thread_id = threading.get_ident()
        while not self._stop.is_set():
            start = time.monotonic()
            with self._lock:
                self._last_tick = start
            await asyncio.sleep(self.interval)
            lag = time.monotonic() - start - self.interval
            with self._lock:
                self._last_tick = time.monotonic()
                stack = self._stall_stack
                self._stall_stack = []
                self._stall_reported = False
            if lag >= self.threshold:
                self._record(lag, stack)

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
        with self._lock:
            self._last_tick = time.monotonic()
        self._task = asyncio.get_running_loop().create_task(self._ticker(), name="loop-lag-probe")
        self._thread = threading.Thread(target=self._watchdog, name="loop-lag-watchdog", daemon=True)
        self._thread.start()
        return True

    async def stop(self) -> None:
        self._stop.set()
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
