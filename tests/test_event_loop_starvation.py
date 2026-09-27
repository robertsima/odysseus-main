"""The UI's polls must not starve the event loop, and agent streams must not
either.

Production, 2026-09-27: with ~15 research workers running, every poll
(`/api/workbench/runs`, `/api/chat/runs`, `/api/research/active`, even the
heartbeat) took 10-50 s and finished in the same millisecond -- the loop was
blocked, not the handlers. The dominant cause was the agent loop resolving a
session (four SQLite statements and a commit) on every streamed text delta of
every headless worker. These tests pin the fixes: that hook no longer touches
the session store, the polls build their answers off the loop once for
concurrent callers, the runs registry is written off the caller's thread, and
a loop stall is reported with where it happened.
"""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from src import poll_coalesce


@pytest.fixture(autouse=True)
def _fresh_poll_cache():
    poll_coalesce._reset_for_tests()
    yield
    poll_coalesce._reset_for_tests()


async def _max_loop_lag(coro, interval=0.01):
    """Run ``coro`` while measuring the worst event-loop lag it caused."""
    lags = []
    done = asyncio.Event()

    async def probe():
        while not done.is_set():
            t = time.perf_counter()
            await asyncio.sleep(interval)
            lags.append(time.perf_counter() - t - interval)

    task = asyncio.create_task(probe())
    try:
        result = await coro
    finally:
        done.set()
        await task
    return result, max(lags or [0.0])


# ── the per-delta compaction hook ────────────────────────────────────────────


class _CountingManager:
    def __init__(self):
        self.calls = 0
        self.session = SimpleNamespace(history=[])

    def get_session(self, session_id):
        self.calls += 1
        return self.session


@pytest.mark.parametrize("state", [None, {}, {"applied": True, "summary": "s", "split_point": 1},
                                   {"summary": None, "split_point": 1}])
def test_per_delta_compaction_hook_does_not_touch_the_session_store_without_a_plan(monkeypatch, state):
    import core.models as models
    from src.context_compactor import apply_compaction_state_for_session

    mgr = _CountingManager()
    monkeypatch.setattr(models, "get_session_manager_instance", lambda: mgr)
    for _ in range(1000):  # one call per streamed delta
        assert apply_compaction_state_for_session("worker-chat", state) is False
    assert mgr.calls == 0


def test_a_pending_plan_is_still_applied_once(monkeypatch):
    import core.models as models
    import src.context_compactor as cc

    mgr = _CountingManager()
    monkeypatch.setattr(models, "get_session_manager_instance", lambda: mgr)
    applied = []
    monkeypatch.setattr(cc, "_update_session_history", lambda session, split, summary, **kw: applied.append(split))
    state = {"summary": "earlier turns", "split_point": 3, "system_msg_count": 1}
    assert cc.apply_compaction_state_for_session("worker-chat", state) is True
    assert cc.apply_compaction_state_for_session("worker-chat", state) is False
    assert applied == [3] and mgr.calls == 1


# ── get_session no longer writes on every read ───────────────────────────────


def test_get_session_touch_is_throttled_per_session(monkeypatch):
    import core.session_manager as sm_mod

    opened = []

    class _FakeDb:
        def query(self, *_a, **_k):
            return self

        def filter(self, *_a, **_k):
            return self

        def first(self):
            return SimpleNamespace(last_accessed=None)

        def commit(self):
            opened.append("commit")

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(sm_mod, "SessionLocal", _FakeDb)
    clock = [1000.0]
    monkeypatch.setattr(sm_mod.time, "monotonic", lambda: clock[0])
    mgr = sm_mod.SessionManager.__new__(sm_mod.SessionManager)
    for _ in range(50):
        mgr._touch_session("a")
    mgr._touch_session("b")
    assert opened == ["commit", "commit"]
    clock[0] += sm_mod.SessionManager.TOUCH_INTERVAL_S
    mgr._touch_session("a")
    assert opened == ["commit", "commit", "commit"]


# ── single-flight answers for the polls ──────────────────────────────────────


async def test_concurrent_identical_polls_build_the_answer_once_off_the_loop():
    calls = []
    main_thread = threading.get_ident()

    def slow_build():
        calls.append(threading.get_ident())
        time.sleep(0.3)  # a registry lock or a busy SQLite read
        return {"runs": [1, 2, 3]}

    async def ask():
        return await poll_coalesce.coalesced(("k", "alice"), slow_build, ttl=0)

    results, lag = await _max_loop_lag(asyncio.gather(*(ask() for _ in range(8))))
    assert len(calls) == 1 and calls[0] != main_thread
    assert all(r == {"runs": [1, 2, 3]} for r in results)
    assert lag < 0.2, f"the loop was blocked {lag:.2f}s"
    # ttl=0: the next poll after completion builds a fresh answer.
    await poll_coalesce.coalesced(("k", "alice"), slow_build, ttl=0)
    assert len(calls) == 2


async def test_ttl_reuses_an_answer_and_keys_do_not_leak():
    calls = []

    def build(tag):
        calls.append(tag)
        return tag

    assert await poll_coalesce.coalesced(("k", "alice"), lambda: build("a"), ttl=5) == "a"
    assert await poll_coalesce.coalesced(("k", "alice"), lambda: build("a2"), ttl=5) == "a"
    assert await poll_coalesce.coalesced(("k", "bob"), lambda: build("b"), ttl=5) == "b"
    poll_coalesce.invalidate("k")
    assert await poll_coalesce.coalesced(("k", "alice"), lambda: build("a3"), ttl=5) == "a3"
    assert calls == ["a", "b", "a3"]


async def test_a_caller_that_gives_up_does_not_cancel_the_others():
    gate = threading.Event()

    def build():
        gate.wait(2)
        return "done"

    first = asyncio.create_task(poll_coalesce.coalesced("k", build, ttl=0))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(poll_coalesce.coalesced("k", build, ttl=0))
    await asyncio.sleep(0.05)
    first.cancel()
    gate.set()
    assert await second == "done"
    with pytest.raises(asyncio.CancelledError):
        await first


async def test_a_failed_build_reaches_every_waiter_and_is_not_cached():
    attempts = []

    def build():
        attempts.append(1)
        time.sleep(0.05)
        raise RuntimeError("db busy")

    results = await asyncio.gather(*(poll_coalesce.coalesced("k", build, ttl=5) for _ in range(3)),
                                   return_exceptions=True)
    assert all(isinstance(r, RuntimeError) for r in results) and len(attempts) == 1
    with pytest.raises(RuntimeError):
        await poll_coalesce.coalesced("k", build, ttl=5)
    assert len(attempts) == 2


# ── the routes use it ────────────────────────────────────────────────────────


async def test_workbench_runs_poll_does_not_block_the_loop_behind_a_slow_registry(monkeypatch):
    from routes import workbench_routes as wr

    monkeypatch.setattr(wr, "_admin", lambda request: "alice")
    calls = []

    def slow_list_runs(**kw):
        calls.append(kw)
        time.sleep(0.4)  # a writer holds the registry lock
        return [{"run_id": "session-1", "status": "running"}]

    monkeypatch.setattr(wr.activity, "list_runs", slow_list_runs)
    router = wr.setup_workbench_routes()
    runs = next(r.endpoint for r in router.routes if r.path == "/api/workbench/runs" and "GET" in r.methods)
    polls = asyncio.gather(*(runs(SimpleNamespace(), session_id="chat-1", limit=50, active=False)
                             for _ in range(6)))
    results, lag = await _max_loop_lag(polls)
    assert [r["runs"][0]["run_id"] for r in results] == ["session-1"] * 6
    assert len(calls) == 1
    assert lag < 0.25, f"the loop was blocked {lag:.2f}s"


# ── the runs registry is written off the caller's thread ─────────────────────


def test_runs_registry_saves_are_coalesced_off_the_callers_thread(tmp_path, monkeypatch):
    from core import atomic_io
    from src import agent_activity as act
    from src import constants

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    act._reset_for_tests()
    writes = []
    real = atomic_io.atomic_write_text

    def counting(path, text):
        writes.append(threading.get_ident())
        real(path, text)

    monkeypatch.setattr(atomic_io, "atomic_write_text", counting)
    try:
        rids = [act.run_started(f"w{i}", "session", f"Worker {i}") for i in range(30)]
        for rid in rids:
            act.note_progress(rid, round=2)
        # Nothing was written inline by the thirty callers ...
        assert writes == [] or threading.get_ident() not in writes
        act.flush_runs()
        # ... and the burst became (at most) a couple of writes.
        assert 1 <= len(writes) <= 3
        assert threading.get_ident() not in writes[:-1]
        act._reset_for_tests()  # restart: the registry was persisted
        assert act.get_run(rids[-1])["status"] == "interrupted"
    finally:
        act._reset_for_tests()


# ── loop lag is reported, with where it happened ─────────────────────────────


async def test_loop_lag_monitor_reports_a_stall_and_the_blocking_frame(caplog):
    from src.loop_lag import LoopLagMonitor

    mon = LoopLagMonitor(threshold=0.2, interval=0.02, report_interval=0)
    assert mon.start()
    try:
        await asyncio.sleep(0.1)

        def block_the_loop_synchronously():
            time.sleep(0.6)

        with caplog.at_level("WARNING", logger="app.loop_lag"):
            block_the_loop_synchronously()
            await asyncio.sleep(0.1)
    finally:
        await mon.stop()
    snap = mon.snapshot()
    assert snap["stalls"] >= 1 and snap["max_lag_s"] >= 0.4
    where = " ".join(snap["last_stall"]["blocked_in"])
    assert "block_the_loop_synchronously" in where
    assert any("[loop-lag] event loop blocked" in r.getMessage() and "block_the_loop_synchronously" in r.getMessage()
               for r in caplog.records)


async def test_loop_lag_monitor_is_quiet_when_the_loop_is_healthy(caplog):
    from src.loop_lag import LoopLagMonitor

    mon = LoopLagMonitor(threshold=0.5, interval=0.02, report_interval=0)
    mon.start()
    try:
        with caplog.at_level("WARNING", logger="app.loop_lag"):
            for _ in range(10):
                await asyncio.sleep(0.02)
    finally:
        await mon.stop()
    assert mon.snapshot()["stalls"] == 0
    assert not [r for r in caplog.records if "[loop-lag]" in r.getMessage()]


def test_loop_lag_monitor_can_be_disabled():
    from src.loop_lag import LoopLagMonitor

    assert LoopLagMonitor(threshold=0).start() is False
