import concurrent.futures
import base64
import json
import threading
import time

import pytest
from sqlalchemy import Column, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import QueuePool

from src.subscription import base


def _jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"x.{payload}.x"


def test_concurrent_refresh_does_not_hold_queuepool_connections(monkeypatch, tmp_path):
    """Network refreshes must not occupy the application's scarce DB pool."""
    mapped = declarative_base()

    class Auth(mapped):
        __tablename__ = "provider_auth_pool_test"
        id = Column(String, primary_key=True)
        provider = Column(String, nullable=False)
        owner = Column(String)
        access_token = Column(String)
        refresh_token = Column(String)
        base_url = Column(String)
        auth_mode = Column(String)
        last_refresh = Column(String)

    engine = create_engine(
        f"sqlite:///{tmp_path / 'pool.db'}",
        connect_args={"check_same_thread": False},
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.15,
    )
    mapped.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add_all([
            Auth(id=f"auth-{i}", provider="test", owner="owner",
                 access_token="spent", refresh_token=f"refresh-{i}",
                 base_url="https://example.invalid", auth_mode="oauth")
            for i in range(8)
        ])
        db.commit()

    monkeypatch.setattr(base, "database_handles", lambda: (Auth, factory, lambda: "now"))

    def resolve(i):
        return base.resolve_runtime_credentials_via_db(
            f"auth-{i}", "owner", provider_id="test",
            default_base_url="https://example.invalid", auth_mode="oauth",
            is_expiring=lambda _token: True,
            refresh=lambda _access, _refresh: (time.sleep(0.25) or {"access_token": f"fresh-{i}"}),
        )["api_key"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        assert list(executor.map(resolve, range(8))) == [f"fresh-{i}" for i in range(8)]

    assert engine.pool.checkedout() == 0
    engine.dispose()


def test_same_auth_waiters_release_pool_and_refresh_once(monkeypatch, tmp_path):
    """Waiters on one rotating credential neither pin the pool nor refresh twice."""
    import src.chatgpt_subscription as legacy

    mapped = declarative_base()

    class Auth(mapped):
        __tablename__ = "same_auth_pool_test"
        id = Column(String, primary_key=True)
        provider = Column(String, nullable=False)
        owner = Column(String)
        access_token = Column(String)
        refresh_token = Column(String)
        base_url = Column(String)
        auth_mode = Column(String)
        last_refresh = Column(String)

    engine = create_engine(
        f"sqlite:///{tmp_path / 'same.db'}", connect_args={"check_same_thread": False},
        poolclass=QueuePool, pool_size=1, max_overflow=0, pool_timeout=0.15,
    )
    mapped.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(Auth(id="shared", provider=legacy.CHATGPT_SUBSCRIPTION_PROVIDER, owner="owner", access_token="spent",
                    refresh_token="rotate-me", base_url="https://example.invalid", auth_mode="oauth"))
        db.commit()
    monkeypatch.setattr(legacy, "_database_handles", lambda: (Auth, factory, lambda: "now"))
    monkeypatch.setattr(legacy, "access_token_is_expiring", lambda token, *_args: token != "fresh")
    calls = 0
    calls_lock = threading.Lock()

    def refresh(_access, _refresh):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.25)
        return {"access_token": "fresh"}

    def resolve(_):
        return legacy.resolve_runtime_credentials("shared", "owner")["api_key"]

    monkeypatch.setattr(legacy, "refresh_oauth_tokens", refresh)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        assert list(executor.map(resolve, range(8))) == ["fresh"] * 8
    assert calls == 1
    assert engine.pool.checkedout() == 0
    with pytest.raises(legacy.ChatGPTSubscriptionAuthNotFound):
        legacy.resolve_runtime_credentials("shared", "different-owner")
    engine.dispose()


def test_chatgpt_endpoint_resolution_releases_single_connection(monkeypatch, tmp_path):
    """Exercise the production ChatGPT adapter through resolve_endpoint."""
    import src.chatgpt_subscription as legacy
    import src.endpoint_resolver as er
    import src.subscription as subscriptions
    from src.subscription import chatgpt as adapter

    mapped = declarative_base()

    class Auth(mapped):
        __tablename__ = "chatgpt_auth_pool_test"
        id = Column(String, primary_key=True)
        provider = Column(String, nullable=False)
        owner = Column(String)
        access_token = Column(String)
        refresh_token = Column(String)
        base_url = Column(String)
        auth_mode = Column(String)
        last_refresh = Column(String)

    class Endpoint(mapped):
        __tablename__ = "chatgpt_endpoint_pool_test"
        id = Column(String, primary_key=True)
        owner = Column(String)
        base_url = Column(String)
        api_key = Column(String)
        provider_auth_id = Column(String)
        cached_models = Column(String)
        models = Column(String)
        pinned_models = Column(String)
        hidden_models = Column(String)
        is_enabled = Column(String)

    engine = create_engine(
        f"sqlite:///{tmp_path / 'endpoint.db'}", connect_args={"check_same_thread": False},
        poolclass=QueuePool, pool_size=1, max_overflow=0, pool_timeout=0.15,
    )
    mapped.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(Auth(id="auth", provider=legacy.CHATGPT_SUBSCRIPTION_PROVIDER, owner="alice",
                    access_token=_jwt(1), refresh_token="rt", base_url="https://chatgpt.com/backend-api/codex",
                    auth_mode="chatgpt"))
        db.add(Endpoint(id="ep", owner="alice", base_url="https://chatgpt.com/backend-api/codex",
                        provider_auth_id="auth", cached_models='["codex"]', is_enabled="1"))
        db.commit()

    monkeypatch.setattr(legacy, "_database_handles", lambda: (Auth, factory, lambda: "now"))
    monkeypatch.setattr(legacy, "refresh_oauth_tokens",
                        lambda *_args, **_kwargs: (time.sleep(0.25) or {"access_token": _jwt(time.time() + 3600)}))
    monkeypatch.setattr(er, "SessionLocal", factory)
    monkeypatch.setattr(er, "ModelEndpoint", Endpoint)
    monkeypatch.setattr(subscriptions, "provider_for_auth_id", lambda *_a, **_k: adapter.provider())
    monkeypatch.setattr("src.settings.load_settings", lambda: {})
    monkeypatch.setattr("src.settings.get_user_setting",
                        lambda key, _owner, default="": "ep" if key == "default_endpoint_id" else ("codex" if key == "default_model" else default))

    url, model, headers = er.resolve_endpoint("default", owner="alice")
    assert url and model == "codex"
    assert "Bearer" in headers.get("Authorization", "")
    assert engine.pool.checkedout() == 0
    engine.dispose()
