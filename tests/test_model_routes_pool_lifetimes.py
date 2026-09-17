import threading
import time
from types import SimpleNamespace

from sqlalchemy import Boolean, Column, DateTime, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import QueuePool

from routes import model_routes


Base = declarative_base()


class PoolModelEndpoint(Base):
    __tablename__ = "pool_model_endpoints"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    base_url = Column(String, nullable=False)
    api_key = Column(String)
    is_enabled = Column(Boolean, default=True)
    cached_models = Column(Text)
    hidden_models = Column(Text)
    pinned_models = Column(Text)
    model_type = Column(String, default="llm")
    supports_tools = Column(Boolean)
    endpoint_kind = Column(String, default="auto")
    model_refresh_mode = Column(String, default="auto")
    model_refresh_interval = Column(String)
    model_refresh_timeout = Column(String)
    owner = Column(String)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)
    provider_auth_id = Column(String)


def _route(router, path, method):
    return next(
        route.endpoint for route in router.routes
        if route.path == path and method in route.methods
    )


def _pool_db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pool.db'}",
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(PoolModelEndpoint(
            id="ep1", name="Endpoint", base_url="http://provider.test/v1",
            api_key="secret", is_enabled=True, cached_models='["cached"]',
            endpoint_kind="proxy", model_refresh_mode="manual",
        ))
        db.commit()
    return engine, factory


def _request():
    return SimpleNamespace(
        headers={},
        state=SimpleNamespace(current_user="admin"),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
    )


def test_probe_selected_releases_connection_while_network_blocks(monkeypatch, tmp_path):
    engine, factory = _pool_db(tmp_path)
    monkeypatch.setattr(model_routes, "SessionLocal", factory)
    monkeypatch.setattr(model_routes, "ModelEndpoint", PoolModelEndpoint)
    monkeypatch.setattr(model_routes, "require_admin", lambda request: None)
    router = model_routes.setup_model_routes(model_discovery=None)
    probe_selected = _route(router, "/api/probe-selected", "POST")
    list_endpoints = _route(router, "/api/model-endpoints", "GET")
    entered = threading.Event()
    release = threading.Event()

    def blocked_probe(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return {"status": "ok"}

    monkeypatch.setattr(model_routes, "_probe_single_model", blocked_probe)
    worker = threading.Thread(target=lambda: probe_selected(
        _request(), {"models": [{"endpoint_id": "ep1", "model": "m1"}]}
    ))
    worker.start()
    assert entered.wait(2)
    assert engine.pool.checkedout() == 0
    assert list_endpoints(_request())[0]["id"] == "ep1"
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    engine.dispose()


def test_manual_refresh_releases_connection_while_network_blocks(monkeypatch, tmp_path):
    engine, factory = _pool_db(tmp_path)
    monkeypatch.setattr(model_routes, "SessionLocal", factory)
    monkeypatch.setattr(model_routes, "ModelEndpoint", PoolModelEndpoint)
    monkeypatch.setattr(model_routes, "require_admin", lambda request: None)
    router = model_routes.setup_model_routes(model_discovery=None)
    refresh = _route(router, "/api/model-endpoints/{ep_id}/models", "GET")
    list_endpoints = _route(router, "/api/model-endpoints", "GET")
    entered = threading.Event()
    release = threading.Event()

    def blocked_probe(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return ["fresh"]

    monkeypatch.setattr(model_routes, "_probe_endpoint", blocked_probe)
    response = SimpleNamespace(headers={})
    worker = threading.Thread(
        target=lambda: refresh("ep1", _request(), response, refresh=True)
    )
    worker.start()
    assert entered.wait(2)
    assert engine.pool.checkedout() == 0
    assert list_endpoints(_request())[0]["id"] == "ep1"
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    engine.dispose()


def test_background_refresh_releases_connection_while_network_blocks(monkeypatch, tmp_path):
    engine, factory = _pool_db(tmp_path)
    monkeypatch.setattr(model_routes, "SessionLocal", factory)
    monkeypatch.setattr(model_routes, "ModelEndpoint", PoolModelEndpoint)
    monkeypatch.setattr(model_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(model_routes, "_auth_disabled", lambda: True)
    monkeypatch.setattr(model_routes, "build_chat_url", lambda base: f"{base}/chat/completions")
    router = model_routes.setup_model_routes(model_discovery=None)
    models = _route(router, "/api/models", "GET")
    list_endpoints = _route(router, "/api/model-endpoints", "GET")
    entered = threading.Event()
    release = threading.Event()

    def blocked_probe(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return ["fresh"]

    monkeypatch.setattr(model_routes, "_probe_endpoint", blocked_probe)
    models(_request(), refresh=True)
    assert entered.wait(2)
    assert engine.pool.checkedout() == 0
    assert list_endpoints(_request())[0]["id"] == "ep1"
    release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with factory() as db:
            if db.get(PoolModelEndpoint, "ep1").cached_models == '["fresh"]':
                break
        time.sleep(0.01)
    else:
        raise AssertionError("background cache write did not finish")
    engine.dispose()


def test_manual_refresh_resolves_credentials_after_releasing_connection(monkeypatch, tmp_path):
    engine, factory = _pool_db(tmp_path)
    monkeypatch.setattr(model_routes, "SessionLocal", factory)
    monkeypatch.setattr(model_routes, "ModelEndpoint", PoolModelEndpoint)
    monkeypatch.setattr(model_routes, "require_admin", lambda request: None)
    router = model_routes.setup_model_routes(model_discovery=None)
    refresh = _route(router, "/api/model-endpoints/{ep_id}/models", "GET")
    list_endpoints = _route(router, "/api/model-endpoints", "GET")
    entered = threading.Event()
    release = threading.Event()

    def blocked_credentials(ep):
        entered.set()
        assert release.wait(2)
        return "resolved-key"

    monkeypatch.setattr(model_routes, "_endpoint_runtime_key", blocked_credentials)
    monkeypatch.setattr(model_routes, "_probe_endpoint", lambda *args, **kwargs: ["fresh"])
    worker = threading.Thread(target=lambda: refresh(
        "ep1", _request(), SimpleNamespace(headers={}), refresh=True
    ))
    worker.start()
    assert entered.wait(2)
    assert engine.pool.checkedout() == 0
    assert list_endpoints(_request())[0]["id"] == "ep1"
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    engine.dispose()


def test_cached_model_list_never_resolves_runtime_credentials(monkeypatch, tmp_path):
    engine, factory = _pool_db(tmp_path)
    monkeypatch.setattr(model_routes, "SessionLocal", factory)
    monkeypatch.setattr(model_routes, "ModelEndpoint", PoolModelEndpoint)
    monkeypatch.setattr(model_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(
        model_routes,
        "_endpoint_runtime_key",
        lambda ep: (_ for _ in ()).throw(AssertionError("cache-only read resolved credentials")),
    )
    router = model_routes.setup_model_routes(model_discovery=None)
    cached = _route(router, "/api/model-endpoints/{ep_id}/models", "GET")
    result = cached("ep1", _request(), SimpleNamespace(headers={}), refresh=False)
    assert [row["id"] for row in result] == ["cached"]
    engine.dispose()


def test_manual_refresh_does_not_persist_result_after_concurrent_endpoint_edit(monkeypatch, tmp_path):
    engine, factory = _pool_db(tmp_path)
    monkeypatch.setattr(model_routes, "SessionLocal", factory)
    monkeypatch.setattr(model_routes, "ModelEndpoint", PoolModelEndpoint)
    monkeypatch.setattr(model_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(model_routes, "_endpoint_runtime_key", lambda ep: "resolved-key")
    router = model_routes.setup_model_routes(model_discovery=None)
    refresh = _route(router, "/api/model-endpoints/{ep_id}/models", "GET")
    entered = threading.Event()
    release = threading.Event()

    def blocked_probe(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return ["stale-result"]

    monkeypatch.setattr(model_routes, "_probe_endpoint", blocked_probe)
    worker = threading.Thread(target=lambda: refresh(
        "ep1", _request(), SimpleNamespace(headers={}), refresh=True
    ))
    worker.start()
    assert entered.wait(2)
    with factory() as db:
        ep = db.get(PoolModelEndpoint, "ep1")
        ep.base_url = "http://new-provider.test/v1"
        db.commit()
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    with factory() as db:
        assert db.get(PoolModelEndpoint, "ep1").cached_models == '["cached"]'
    engine.dispose()


def test_probe_routes_resolve_credentials_after_releasing_connection(monkeypatch, tmp_path):
    engine, factory = _pool_db(tmp_path)
    monkeypatch.setattr(model_routes, "SessionLocal", factory)
    monkeypatch.setattr(model_routes, "ModelEndpoint", PoolModelEndpoint)
    monkeypatch.setattr(model_routes, "require_admin", lambda request: None)
    router = model_routes.setup_model_routes(model_discovery=None)
    routes = [
        (_route(router, "/api/probe", "GET"), lambda fn: fn(_request(), endpoint_id=None)),
        (_route(router, "/api/model-endpoints/{ep_id}/probe", "GET"),
         lambda fn: fn("ep1", _request())),
    ]

    for endpoint, invoke in routes:
        entered = threading.Event()
        release = threading.Event()

        def blocked_credentials(ep):
            entered.set()
            assert engine.pool.checkedout() == 0
            assert release.wait(2)
            return "resolved-key"

        monkeypatch.setattr(model_routes, "_endpoint_runtime_key", blocked_credentials)
        monkeypatch.setattr(model_routes, "_probe_endpoint", lambda *args, **kwargs: [])
        worker = threading.Thread(target=lambda: invoke(endpoint))
        worker.start()
        assert entered.wait(2)
        assert engine.pool.checkedout() == 0
        with factory() as db:
            assert db.get(PoolModelEndpoint, "ep1") is not None
        release.set()
        worker.join(2)
        assert not worker.is_alive()
    engine.dispose()


def test_background_refresh_discards_result_after_concurrent_endpoint_edit(monkeypatch, tmp_path):
    engine, factory = _pool_db(tmp_path)
    monkeypatch.setattr(model_routes, "SessionLocal", factory)
    monkeypatch.setattr(model_routes, "ModelEndpoint", PoolModelEndpoint)
    monkeypatch.setattr(model_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(model_routes, "_auth_disabled", lambda: True)
    monkeypatch.setattr(model_routes, "_endpoint_runtime_key", lambda ep: "resolved-key")
    router = model_routes.setup_model_routes(model_discovery=None)
    models = _route(router, "/api/models", "GET")
    entered = threading.Event()
    release = threading.Event()

    def blocked_probe(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return ["stale-result"]

    monkeypatch.setattr(model_routes, "_probe_endpoint", blocked_probe)
    models(_request(), refresh=True)
    assert entered.wait(2)
    with factory() as db:
        ep = db.get(PoolModelEndpoint, "ep1")
        ep.base_url = "http://replacement.test/v1"
        db.commit()
    release.set()
    # The background worker's write phase is deliberately short. Give it a
    # scheduling turn, then verify the final persisted value after the pool is
    # fully idle (not while this test's own read session is checked out).
    time.sleep(0.1)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with factory() as db:
            ep = db.get(PoolModelEndpoint, "ep1")
            base_url, cached_models = ep.base_url, ep.cached_models
        if engine.pool.checkedout() == 0 and base_url == "http://replacement.test/v1":
            assert cached_models == '["cached"]'
            break
        time.sleep(0.01)
    else:
        raise AssertionError("background refresh did not finish")
    engine.dispose()


def test_background_stale_cookbook_disable_is_persisted(monkeypatch, tmp_path):
    engine, factory = _pool_db(tmp_path)
    with factory() as db:
        db.add(PoolModelEndpoint(
            id="local-stale", name="Stale serve", base_url="http://localhost:9999/v1",
            is_enabled=True, cached_models='["old"]', endpoint_kind="local",
            model_refresh_mode="auto",
        ))
        db.commit()
    monkeypatch.setattr(model_routes, "SessionLocal", factory)
    monkeypatch.setattr(model_routes, "ModelEndpoint", PoolModelEndpoint)
    monkeypatch.setattr(model_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(model_routes, "_auth_disabled", lambda: True)
    monkeypatch.setattr(model_routes, "_active_cookbook_endpoint_ids", lambda: {"local-active"})
    monkeypatch.setattr(model_routes, "_load_settings", lambda: {})
    monkeypatch.setattr(model_routes, "_endpoint_runtime_key", lambda ep: "resolved-key")
    monkeypatch.setattr(model_routes, "_probe_endpoint", lambda *args, **kwargs: [])
    router = model_routes.setup_model_routes(model_discovery=None)
    models = _route(router, "/api/models", "GET")
    models(_request(), refresh=True)

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with factory() as db:
            stale = db.get(PoolModelEndpoint, "local-stale")
            if stale and not stale.is_enabled:
                assert stale.model_refresh_mode == "disabled"
                break
        time.sleep(0.01)
    else:
        raise AssertionError("stale cookbook endpoint disable was not persisted")
    engine.dispose()
