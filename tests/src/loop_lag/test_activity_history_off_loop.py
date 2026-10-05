"""GET /api/workbench/activity must not parse timelines on the event loop, and
a loop stall is never reported as ``blocked_in=unknown``.

Production (2026-09-27 22:43:22)::

    [loop-lag] event loop blocked 1.03s (stalls=2, worst=1.38s)
        blocked_in=agent_activity.py:147:_load_session <- agent_activity.py:531:history
                   <- workbench_routes.py:89:activity_history

The route JSON-decoded a chat's whole timeline file (up to 2 MB) on the loop,
under the lock every activity publisher takes. And two startup stalls (2.93 s,
1.14 s) were logged as ``blocked_in=unknown``: the sampled stack had no
project frame, so the report said nothing at all.
"""
import asyncio
import gc
import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from src import agent_activity as act
from src import constants, poll_coalesce


@pytest.fixture
def activity_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    act._reset_for_tests()
    poll_coalesce._reset_for_tests()
    yield tmp_path
    act._reset_for_tests()
    poll_coalesce._reset_for_tests()


def _write_timeline(sid, count):
    import os

    os.makedirs(act.activity_dir(), exist_ok=True)
    with open(act._session_file(sid), "w", encoding="utf-8") as fh:
        for seq in range(1, count + 1):
            fh.write(json.dumps({"seq": seq, "ts": float(seq), "title": f"ev {seq}"}) + "\n")
        fh.write("not json\n")


# ── bounded load ────────────────────────────────────────────────────────────


def test_history_keeps_the_newest_events_and_the_true_last_seq(activity_dir):
    _write_timeline("chat-1", act.MAX_EVENTS_PER_SESSION * 3)
    rows = act.history("chat-1", limit=act.MAX_EVENTS_PER_SESSION)
    top = act.MAX_EVENTS_PER_SESSION * 3
    assert [r["seq"] for r in rows] == list(range(top - act.MAX_EVENTS_PER_SESSION + 1, top + 1))
    assert act.last_seq("chat-1") == top
    assert [r["seq"] for r in act.history("chat-1", since_seq=top - 2)] == [top - 1, top]
    # A new event continues the sequence rather than restarting it.
    ev = act.publish("chat-1", "note", "after reload")
    assert ev["seq"] == top + 1


def test_load_parses_only_the_lines_it_keeps(activity_dir, monkeypatch):
    _write_timeline("chat-1", act.MAX_EVENTS_PER_SESSION * 4)
    parsed = []
    real_loads = json.loads

    def counting_loads(raw, *a, **k):
        parsed.append(1)
        return real_loads(raw, *a, **k)

    monkeypatch.setattr(act.json, "loads", counting_loads)
    act.history("chat-1")
    # The trailing garbage line plus the kept events: not the whole file.
    assert len(parsed) <= act.MAX_EVENTS_PER_SESSION + 1


def test_the_file_is_read_outside_the_activity_lock(activity_dir, monkeypatch):
    _write_timeline("chat-1", 10)
    held = []
    real = act._read_tail

    def spy(sid):
        # RLock: a probe from another thread tells whether anyone holds it.
        probe = []

        def try_lock():
            got = act._lock.acquire(timeout=0.5)
            probe.append(got)
            if got:
                act._lock.release()

        t = threading.Thread(target=try_lock)
        t.start()
        t.join()
        held.append(not probe[0])
        return real(sid)

    monkeypatch.setattr(act, "_read_tail", spy)
    assert len(act.history("chat-1")) == 10
    assert held == [False]


def test_global_history_reads_only_the_most_recent_timelines(activity_dir, monkeypatch):
    import os

    monkeypatch.setattr(act, "GLOBAL_HISTORY_MAX_SESSIONS", 2)
    for i, sid in enumerate(("old", "mid", "new")):
        _write_timeline(sid, 3)
        os.utime(act._session_file(sid), (1000 + i, 1000 + i))
    act.history(act.GLOBAL_FEED)
    assert "old" not in act._loaded and {"mid", "new"} <= act._loaded


# ── the route builds it off the loop ────────────────────────────────────────


async def test_activity_route_does_not_block_the_loop(activity_dir, monkeypatch):
    from routes import workbench_routes as wr

    monkeypatch.setattr(wr, "_admin", lambda request: "admin")
    loop_thread = threading.get_ident()
    threads = []

    def slow_history(session_id, *, since_seq=0, limit=200):
        threads.append(threading.get_ident())
        time.sleep(0.4)  # a big timeline being parsed
        return [{"seq": 1}]

    monkeypatch.setattr(wr.activity, "history", slow_history)
    monkeypatch.setattr(wr.activity, "last_seq", lambda sid: 1)
    router = wr.setup_workbench_routes()
    endpoint = next(r.endpoint for r in router.routes
                    if r.path == "/api/workbench/activity" and "GET" in r.methods)

    lags, done = [], asyncio.Event()

    async def probe():
        while not done.is_set():
            t = time.perf_counter()
            await asyncio.sleep(0.01)
            lags.append(time.perf_counter() - t - 0.01)

    task = asyncio.create_task(probe())
    try:
        results = await asyncio.gather(*(endpoint(SimpleNamespace(), session_id="chat-1", since=0, limit=200)
                                         for _ in range(4)))
    finally:
        done.set()
        await task
    assert all(r == {"session_id": "chat-1", "events": [{"seq": 1}], "seq": 1} for r in results)
    assert len(threads) == 1 and loop_thread not in threads  # once, in a worker thread
    assert max(lags) < 0.25


# ── loop-lag never says "unknown" ───────────────────────────────────────────


def _frame_in(filename, func="blocking_call", parent=None):
    code = compile(f"def {func}():\n    return __import__('sys')._getframe()\n", filename, "exec")
    ns = {}
    exec(code, ns)
    return ns[func]()


def test_a_stall_entirely_in_library_code_names_the_library_frame():
    from src import loop_lag

    frame = _frame_in("/usr/lib/python3.14/site-packages/anyio/_backends/_asyncio.py", "run_sync")
    stack = loop_lag.app_stack(frame)
    assert stack and stack[0].startswith("lib:anyio/_backends/_asyncio.py:")
    assert stack[0].endswith(":run_sync")


def test_an_app_stack_blocked_inside_a_library_leads_with_that_library_call():
    from src import loop_lag

    def app_caller():
        return _frame_in("/usr/lib/python3.14/site-packages/sqlalchemy/orm/session.py", "commit")

    stack = loop_lag.app_stack(app_caller())
    assert stack[0].startswith("lib:sqlalchemy/orm/session.py:") and stack[0].endswith(":commit")
    assert any(s.endswith(":app_caller") for s in stack[1:])


async def test_a_block_right_after_start_is_sampled_before_the_first_tick():
    """Startup work that blocks the loop right after the monitor starts: the
    ticker has not run yet, so the watchdog did not know which thread to
    sample and the stall was reported as unknown."""
    from src.loop_lag import LoopLagMonitor

    mon = LoopLagMonitor(threshold=0.2, interval=0.02, report_interval=0)
    assert mon.start()
    try:
        def startup_blocker():
            time.sleep(0.6)

        startup_blocker()  # no await between start() and the block
        await asyncio.sleep(0.1)
    finally:
        await mon.stop()
    where = " ".join(mon.snapshot()["last_stall"]["blocked_in"])
    assert "startup_blocker" in where


def test_an_unsampled_stall_says_why_instead_of_unknown():
    from src.loop_lag import LoopLagMonitor

    mon = LoopLagMonitor(threshold=0.2, interval=0.02, report_interval=0)

    async def main():
        mon._loop_thread_id = threading.get_ident()
        mon._stop.clear()
        task = asyncio.create_task(mon._ticker())  # ticker only: no watchdog samples
        await asyncio.sleep(0.05)
        time.sleep(0.4)
        await asyncio.sleep(0.05)
        mon._stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())
    blocked_in = mon.snapshot()["last_stall"]["blocked_in"]
    assert blocked_in and blocked_in[0].startswith("unsampled(")


def test_a_gc_pause_is_named():
    from src.loop_lag import LoopLagMonitor

    mon = LoopLagMonitor(threshold=0.2, interval=0.02, report_interval=0)

    async def main():
        mon._loop_thread_id = threading.get_ident()
        mon._stop.clear()
        task = asyncio.create_task(mon._ticker())
        await asyncio.sleep(0.05)
        mon._gc_callback("start", {"generation": 2})
        time.sleep(0.4)  # stands in for a long collection
        mon._gc_callback("stop", {"generation": 2})
        await asyncio.sleep(0.05)
        mon._stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())
    assert mon.snapshot()["last_stall"]["blocked_in"][0].startswith("gc(generation=2, ")


async def test_stop_removes_the_gc_callback():
    from src.loop_lag import LoopLagMonitor

    mon = LoopLagMonitor(threshold=5, interval=0.02, report_interval=0)
    mon.start()
    assert mon._gc_callback in gc.callbacks
    await mon.stop()
    assert mon._gc_callback not in gc.callbacks
