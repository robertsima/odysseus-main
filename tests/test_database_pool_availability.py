"""Real, deliberately scarce connections must remain available during remote work."""
import asyncio

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

import core.database as database
from core.database_health import PoolDiagnostics, install_database_error_handler
from src import session_actions


@pytest.fixture
def pool(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'availability.db'}",
        connect_args={"check_same_thread": False},
        poolclass=QueuePool, pool_size=1, max_overflow=0, pool_timeout=0.05,
    )
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    with factory() as db:
        db.add_all([
            database.Session(id=f"session-{i}", owner="alice", name=f"Project {i}",
                             endpoint_url="", model="", is_important=True)
            for i in range(2)
        ])
        db.commit()
    yield engine, factory
    assert engine.pool.checkedout() == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_auto_sort_releases_connection_and_preserves_intervening_user_move(pool, monkeypatch):
    from src import llm_core, task_endpoint
    engine, factory = pool
    entered, release = asyncio.Event(), asyncio.Event()

    def endpoint(**kwargs):
        assert engine.pool.checkedout() == 0
        return "https://unused.invalid", "model", {}

    async def model(*args, **kwargs):
        assert engine.pool.checkedout() == 0
        entered.set()
        await release.wait()
        return '{"folders":{"AI folder":["session-0","session-1"]}}'

    monkeypatch.setattr(task_endpoint, "resolve_task_endpoint", endpoint)
    monkeypatch.setattr(llm_core, "llm_call_async", model)
    worker = asyncio.create_task(session_actions.run_auto_sort("alice"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with factory() as db:
            assert db.query(database.Session).count() == 2
            db.query(database.Session).filter_by(id="session-0").one().folder = "User choice"
            db.commit()
        release.set()
        result = await asyncio.wait_for(worker, 2)
        assert "Sorted 1 sessions" in result
        with factory() as db:
            assert db.query(database.Session).filter_by(id="session-0").one().folder == "User choice"
            assert db.query(database.Session).filter_by(id="session-1").one().folder == "AI folder"
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["error", "cancel"])
async def test_auto_sort_error_or_cancellation_releases_pool(pool, monkeypatch, mode):
    from src import llm_core, task_endpoint
    engine, factory = pool
    entered = asyncio.Event()
    monkeypatch.setattr(task_endpoint, "resolve_task_endpoint", lambda **kw: ("unused", "model", {}))

    async def model(*args, **kwargs):
        entered.set()
        if mode == "error":
            raise RuntimeError("provider unavailable")
        await asyncio.Event().wait()

    monkeypatch.setattr(llm_core, "llm_call_async", model)
    worker = asyncio.create_task(session_actions.run_auto_sort("alice"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert engine.pool.checkedout() == 0
        if mode == "cancel":
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        with factory() as db:
            assert db.query(database.Session).count() == 2
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


def test_pool_diagnostics_are_query_free_and_release_records(pool):
    engine, _ = pool
    diagnostics = PoolDiagnostics(engine)
    with engine.connect() as connection:
        connection.execute(text("select 1"))
        snapshot = diagnostics.snapshot()  # the only connection is occupied
        assert snapshot["active_count"] == 1
        assert "test_database_pool_availability.py" in snapshot["oldest_holders"][0]["origin"]
        assert "select 1" not in str(snapshot)
        connection.invalidate()
        assert diagnostics.snapshot()["active_count"] == 0
    assert diagnostics.snapshot()["active_count"] == 0


@pytest.mark.asyncio
async def test_database_pool_timeout_returns_safe_retryable_503(pool, caplog):
    engine, _ = pool
    app = FastAPI()
    diagnostics = PoolDiagnostics(engine)
    install_database_error_handler(app, diagnostics)

    @app.get("/query")
    def query():
        with engine.connect() as connection:
            return {"value": connection.execute(text("select 1")).scalar()}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        with engine.connect():
            result = await client.get("/query")
            assert result.status_code == 503
            assert result.json()["error"] == "DATABASE_BUSY"
            assert result.headers["Retry-After"] == "5"
            assert "oldest_holders" not in result.text
            assert "[db-pool] checkout timed out occupancy=" in caplog.text
        recovered = await client.get("/query")
        assert recovered.status_code == 200 and recovered.json() == {"value": 1}
