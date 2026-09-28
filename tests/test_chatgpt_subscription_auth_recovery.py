"""ChatGPT Subscription: single-flight refresh, 401 recovery, honest errors.

Regression for the 2026-09-27 production log: three research workers launched
by one orchestrate_agents call sent their first request within half a second
with the same (valid) bearer; one or two got ``401 {"detail":"Unauthorized"}``
while the others got 200, each 401 killed its worker at round 1, and the error
told the user to reconnect a provider whose credentials were fine.

A fake OAuth token endpoint (rotating refresh tokens, rejecting reuse) and a
fake Codex responses endpoint stand in for OpenAI; a throwaway SQLite table
stands in for ``ProviderAuthSession``.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import itertools
import json
import logging
import threading
import time

import httpx
import pytest
from sqlalchemy import Column, DateTime, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from src import chatgpt_subscription as cs
from src import llm_core

CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
_nonce = itertools.count()


def _jwt(expires_in: int = 3600, account: str = "acct-1") -> str:
    claims = {
        "exp": int(time.time()) + expires_in,
        "sub": "user-1",
        "jti": f"n{next(_nonce)}",
        "https://api.openai.com/auth": {"chatgpt_account_id": account},
    }
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"hdr.{body}.sig"


class FakeOAuth:
    """Token endpoint with rotating refresh tokens: a reused one is invalid_grant."""

    def __init__(self, refresh_token: str, *, delay: float = 0.0, fail_with: str = ""):
        self.valid_refresh = refresh_token
        self.delay = delay
        self.fail_with = fail_with
        self.calls = 0
        self.issued: list[str] = []
        self._lock = threading.Lock()

    def post(self, url, headers=None, data=None, timeout=None, **kwargs):
        assert url == cs.CHATGPT_OAUTH_TOKEN_URL
        request = httpx.Request("POST", url)
        with self._lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            if self.fail_with or data.get("refresh_token") != self.valid_refresh:
                return httpx.Response(400, json={
                    "error": self.fail_with or "refresh_token_reused",
                    "error_description": "Your refresh token has already been used.",
                }, request=request)
            access, refresh = _jwt(), f"rt-{next(_nonce)}"
            self.valid_refresh = refresh
            self.issued += [access, refresh]
        return httpx.Response(200, json={"access_token": access, "refresh_token": refresh},
                              request=request)


@pytest.fixture
def store(monkeypatch, tmp_path):
    """One stored ChatGPT credential in a private SQLite table."""
    mapped = declarative_base()

    class Auth(mapped):
        __tablename__ = "provider_auth_sessions_test"
        id = Column(String, primary_key=True)
        provider = Column(String, nullable=False)
        owner = Column(String)
        base_url = Column(String)
        access_token = Column(String)
        refresh_token = Column(String)
        last_refresh = Column(DateTime)
        auth_mode = Column(String)

    engine = create_engine(f"sqlite:///{tmp_path / 'auth.db'}",
                           connect_args={"check_same_thread": False})
    mapped.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    access, refresh = _jwt(), "rt-initial"
    with factory() as db:
        db.add(Auth(id="auth-abc12345", provider=cs.CHATGPT_SUBSCRIPTION_PROVIDER, owner="robert",
                    base_url=cs.DEFAULT_CHATGPT_SUBSCRIPTION_BASE_URL, access_token=access,
                    refresh_token=refresh, auth_mode="chatgpt"))
        db.commit()

    from datetime import datetime, timezone

    def utcnow_naive():
        return datetime.now(timezone.utc).replace(tzinfo=None)

    monkeypatch.setattr(cs, "_database_handles", lambda: (Auth, factory, utcnow_naive))
    monkeypatch.setattr(cs, "_LAST_REFRESH_AT", {})
    monkeypatch.setattr(cs, "_SUPERSEDED_TOKENS", type(cs._SUPERSEDED_TOKENS)())
    oauth = FakeOAuth(refresh)
    monkeypatch.setattr(cs.httpx, "post", oauth.post)

    class Store:
        def row(self):
            with factory() as db:
                r = db.query(Auth).one()
                return {"access_token": r.access_token, "refresh_token": r.refresh_token}

    s = Store()
    s.initial_access, s.oauth, s.auth_id = access, oauth, "auth-abc12345"
    yield s
    engine.dispose()


# ── refresh machinery ────────────────────────────────────────────────────


def test_skew_is_five_minutes():
    assert cs.CHATGPT_ACCESS_TOKEN_REFRESH_SKEW_SECONDS == 300
    assert cs.access_token_is_expiring(_jwt(expires_in=240)) is True
    assert cs.access_token_is_expiring(_jwt(expires_in=600)) is False


def test_concurrent_expiring_resolves_refresh_once(store, monkeypatch):
    """Eight requests find the token inside the skew at once: one refresh."""
    Auth, factory, _ = cs._database_handles()
    with factory() as db:
        db.query(Auth).one().access_token = _jwt(expires_in=60)
        db.commit()
    store.oauth.delay = 0.15

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(lambda _i: cs.resolve_runtime_credentials(store.auth_id, "robert")["api_key"],
                               range(8)))

    assert store.oauth.calls == 1
    assert len(set(tokens)) == 1
    assert tokens[0] == store.row()["access_token"] == store.oauth.issued[0]


def test_concurrent_401_recoveries_refresh_once(store):
    """A burst of 401s on the same token: one refresh, everyone gets its result.

    Without single-flight the second refresh would present an already-rotated
    refresh token, get refresh_token_reused, and tell the user to reconnect.
    """
    store.oauth.delay = 0.15
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        tokens = list(pool.map(lambda _i: cs.recover_rejected_access_token(store.initial_access),
                               range(6)))

    assert store.oauth.calls == 1
    assert set(tokens) == {store.row()["access_token"]}
    assert store.row()["access_token"] != store.initial_access


def test_401_on_freshly_minted_token_does_not_refresh_again(store):
    fresh = cs.recover_rejected_access_token(store.initial_access)
    assert store.oauth.calls == 1
    # The new token is rejected too, seconds later: minting another will not
    # help and would only rotate the refresh token again.
    assert cs.recover_rejected_access_token(fresh) == fresh
    assert store.oauth.calls == 1


def test_refresh_is_logged_without_token_values(store, caplog):
    caplog.set_level(logging.INFO, logger="src.chatgpt_subscription")
    cs.recover_rejected_access_token(store.initial_access)
    text = caplog.text
    assert "[chatgpt-auth] refresh start auth=auth-abc reason=unauthorized" in text
    assert "[chatgpt-auth] refresh ok auth=auth-abc reason=unauthorized" in text
    for secret in [store.initial_access, "rt-initial", *store.oauth.issued]:
        assert secret not in text


def test_refresh_failure_is_logged_without_token_values(store, caplog):
    caplog.set_level(logging.INFO, logger="src.chatgpt_subscription")
    store.oauth.fail_with = "invalid_grant"
    with pytest.raises(cs.ChatGPTSubscriptionReauthRequired):
        cs.recover_rejected_access_token(store.initial_access)
    assert "[chatgpt-auth] refresh failed auth=auth-abc reason=unauthorized" in caplog.text
    assert store.initial_access not in caplog.text and "rt-initial" not in caplog.text


# ── request path: stream_llm ─────────────────────────────────────────────


class FakeCodex:
    """Responses endpoint that rejects the bearers in ``reject`` with a 401."""

    def __init__(self, reject=(), reject_all=False):
        self.reject = set(reject)
        self.reject_all = reject_all
        self.bearers: list[str] = []

    def stream(self, method, url, headers=None, **kwargs):
        bearer = (headers or {}).get("Authorization", "")[len("Bearer "):]
        self.bearers.append(bearer)
        rejected = self.reject_all or bearer in self.reject

        class _Resp:
            status_code = 401 if rejected else 200

            async def aread(self):
                return b'{"detail":"Unauthorized"}'

            async def aiter_lines(self):
                for event in ({"type": "response.output_text.delta", "delta": "hello"},
                              {"type": "response.completed", "response": {}}):
                    yield f"data: {json.dumps(event)}"

        class _Ctx:
            async def __aenter__(self):
                return _Resp()

            async def __aexit__(self, *a):
                return False

        return _Ctx()


@pytest.fixture
def codex(monkeypatch):
    def install(fake):
        monkeypatch.setattr(llm_core, "_get_http_client", lambda: fake)
        return fake

    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda url: None)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_CHATGPT_AUTH_RETRY_DELAY", (0, 0))
    return install


async def _stream(token):
    return [c async for c in llm_core.stream_llm(
        CODEX_URL, "gpt-5.6-sol", [{"role": "user", "content": "research"}],
        headers={"Authorization": f"Bearer {token}"},
    )]


def _errors(chunks):
    return [json.loads(c.split("data: ", 1)[1]) for c in chunks if c.startswith("event: error")]


async def test_401_refreshes_and_retries_once_then_succeeds(store, codex):
    fake = codex(FakeCodex(reject={store.initial_access}))

    chunks = await _stream(store.initial_access)

    assert _errors(chunks) == []
    assert any('"delta": "hello"' in c for c in chunks)
    assert store.oauth.calls == 1
    assert fake.bearers == [store.initial_access, store.row()["access_token"]]


async def test_concurrent_workers_401_share_one_refresh(store, codex):
    """The production shape: parallel workers, same token, 401s in one burst."""
    store.oauth.delay = 0.1
    fake = codex(FakeCodex(reject={store.initial_access}))

    results = await asyncio.gather(*(_stream(store.initial_access) for _ in range(3)))

    assert all(_errors(chunks) == [] for chunks in results)
    assert store.oauth.calls == 1
    assert fake.bearers.count(store.row()["access_token"]) == 3


async def test_transient_401_with_valid_token_retries_same_token(store, codex):
    """A 401 on a token that was just minted retries without another refresh."""
    first = cs.recover_rejected_access_token(store.initial_access)
    assert store.oauth.calls == 1

    class Flaky(FakeCodex):
        def stream(self, method, url, headers=None, **kwargs):
            self.reject_all = not self.bearers  # only the first attempt fails
            return super().stream(method, url, headers=headers, **kwargs)

    fake = codex(Flaky())
    chunks = await _stream(first)

    assert _errors(chunks) == []
    assert fake.bearers == [first, first]
    assert store.oauth.calls == 1


async def test_second_401_is_reported_without_reconnect_advice(store, codex):
    fake = codex(FakeCodex(reject_all=True))

    chunks = await _stream(store.initial_access)

    [error] = _errors(chunks)
    assert error["status"] == 401
    assert "Reconnect" not in error["text"]
    assert "retry" in error["text"].lower()
    assert len(fake.bearers) == 2  # exactly one replay
    assert store.oauth.calls == 1


async def test_refresh_failure_says_reconnect(store, codex):
    store.oauth.fail_with = "invalid_grant"
    fake = codex(FakeCodex(reject_all=True))

    chunks = await _stream(store.initial_access)

    [error] = _errors(chunks)
    assert error["status"] == 401
    assert error["text"].count("Reconnect the provider") == 1
    assert len(fake.bearers) == 1  # nothing to replay with


async def test_refresh_network_failure_does_not_say_reconnect(store, codex, monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("token endpoint unreachable")

    monkeypatch.setattr(cs.httpx, "post", boom)
    codex(FakeCodex(reject_all=True))

    [error] = _errors(await _stream(store.initial_access))
    assert "Reconnect" not in error["text"]
    assert "retry" in error["text"].lower()


async def test_stale_snapshot_is_swapped_before_sending(store, codex):
    """Headers snapshotted before another path refreshed carry the new token."""
    fresh = cs.recover_rejected_access_token(store.initial_access)
    fake = codex(FakeCodex(reject={store.initial_access}))

    chunks = await _stream(store.initial_access)

    assert _errors(chunks) == []
    assert fake.bearers == [fresh]  # no 401 round trip at all


async def test_expiring_snapshot_is_refreshed_before_sending(store, codex):
    Auth, factory, _ = cs._database_handles()
    expiring = _jwt(expires_in=60)
    with factory() as db:
        db.query(Auth).one().access_token = expiring
        db.commit()
    fake = codex(FakeCodex(reject={expiring}))

    chunks = await _stream(expiring)

    assert _errors(chunks) == []
    assert store.oauth.calls == 1
    assert fake.bearers == [store.row()["access_token"]]


async def test_llm_call_async_recovers_too(store, codex):
    codex(FakeCodex(reject={store.initial_access}))
    text = await llm_core.llm_call_async(
        CODEX_URL, "gpt-5.6-sol", [{"role": "user", "content": "title this"}],
        headers={"Authorization": f"Bearer {store.initial_access}"},
    )
    assert text == "hello"
    assert store.oauth.calls == 1


def test_formatter_no_longer_blames_credentials_for_a_401():
    text = llm_core._format_chatgpt_subscription_error(401, '{"detail":"Unauthorized"}')
    assert "Reconnect" not in text and "expired" not in text
