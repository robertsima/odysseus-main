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


# ── 2026-09-28: workflow children sent their first request with no bearer ──
# A metadata refresh (`get_session` on the child chat) replaced the child's
# request-local bearer with the row's empty headers; the 401 recovery then
# returned silently because there was no bearer to look up. Zero
# `[chatgpt-auth]` lines for three dead workers.


async def _stream_with(headers, session_id=None):
    return [c async for c in llm_core.stream_llm(
        CODEX_URL, "gpt-6-sol", [{"role": "user", "content": "research"}],
        headers=headers, session_id=session_id,
    )]


@pytest.fixture
def owner_ref(monkeypatch, store):
    """The child chat's endpoint resolves to the stored credential."""
    seen = []

    def ref(session_id):
        seen.append(session_id)
        return (store.auth_id, "robert") if session_id == "child-1" else None

    monkeypatch.setattr(cs, "_session_credential_ref", ref)
    return seen


async def test_bearerless_request_gets_the_chat_owners_token_before_sending(store, codex, owner_ref, caplog):
    caplog.set_level(logging.INFO)
    fake = codex(FakeCodex(reject={""}))

    chunks = await _stream_with(cs.chatgpt_headers(None), session_id="child-1")

    assert _errors(chunks) == []
    assert fake.bearers == [store.initial_access]  # no 401 round trip at all
    assert "[chatgpt-auth] request for session=child-1 carries no bearer token" in caplog.text
    assert "missing bearer session=child-1: resolved the chat owner's stored credential" in caplog.text
    assert store.initial_access not in caplog.text


async def test_bearerless_401_recovers_through_the_session_owner(store, codex, owner_ref, monkeypatch, caplog):
    """The recovery path itself, when the pre-send resolution did not help."""
    caplog.set_level(logging.INFO)

    async def no_presend(headers, session_id=None):
        return headers, None

    monkeypatch.setattr(llm_core, "_chatgpt_presend_headers", no_presend)
    fake = codex(FakeCodex(reject={""}))

    chunks = await _stream_with({}, session_id="child-1")

    assert _errors(chunks) == []
    assert fake.bearers == ["", store.initial_access]
    assert "401 model=gpt-6-sol session=child-1: the request carried no bearer token" in caplog.text
    assert "retrying once with the owner's stored token" in caplog.text


async def test_bearerless_401_with_no_owner_credential_is_logged_and_reported(store, codex, owner_ref, caplog):
    caplog.set_level(logging.INFO)
    fake = codex(FakeCodex(reject_all=True))

    chunks = await _stream_with({}, session_id="someone-else")

    [error] = _errors(chunks)
    assert error["status"] == 401
    assert error["text"] == llm_core._CHATGPT_401_NO_BEARER
    assert fake.bearers == [""]
    for line in ("carries no bearer token",
                 "missing bearer session=someone-: no enabled ChatGPT Subscription endpoint",
                 "the request carried no bearer token",
                 "no credential to retry with; reporting the 401"):
        assert line in caplog.text


async def test_unmatched_token_401_retries_with_the_session_owners_token(store, codex, owner_ref, caplog):
    caplog.set_level(logging.INFO)
    foreign = _jwt(account="acct-somebody-else")
    fake = codex(FakeCodex(reject={foreign}))

    chunks = await _stream_with({"Authorization": f"Bearer {foreign}"}, session_id="child-1")

    assert _errors(chunks) == []
    assert fake.bearers == [foreign, store.initial_access]
    assert "401 recovery: no stored credential matches the rejected token" in caplog.text
    assert "the rejected token matches no stored credential; resolving the chat owner's current one" in caplog.text
    assert store.oauth.calls == 0  # the owner's token was fine; nothing to refresh


async def test_unmatched_token_without_a_session_logs_every_branch(store, codex, caplog):
    caplog.set_level(logging.INFO)
    foreign = _jwt(account="acct-somebody-else")
    fake = codex(FakeCodex(reject_all=True))

    chunks = await _stream_with({"Authorization": f"Bearer {foreign}"})

    [error] = _errors(chunks)
    assert error["text"] == llm_core._CHATGPT_401_UNMATCHED
    assert len(fake.bearers) == 1
    for line in ("no stored credential matches the rejected token",
                 "unmatched bearer: no chat session on the request",
                 "no credential to retry with; reporting the 401"):
        assert line in caplog.text


async def test_owner_token_that_is_the_rejected_one_is_not_replayed(store, codex, monkeypatch):
    """Nothing newer to send: one request, and the 401 is reported."""
    monkeypatch.setattr(cs, "_find_auth_for_token", lambda token: None)
    monkeypatch.setattr(cs, "_session_credential_ref", lambda sid: (store.auth_id, "robert"))
    fake = codex(FakeCodex(reject_all=True))

    chunks = await _stream_with({"Authorization": f"Bearer {store.initial_access}"}, session_id="child-1")

    [error] = _errors(chunks)
    assert error["status"] == 401 and len(fake.bearers) == 1


def test_session_credential_ref_uses_the_chats_owner_and_endpoint(monkeypatch, tmp_path):
    from sqlalchemy.orm import sessionmaker as _sessionmaker

    import core.database as database

    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}", connect_args={"check_same_thread": False})
    database.Base.metadata.create_all(engine, tables=[database.Session.__table__,
                                                      database.ModelEndpoint.__table__])
    factory = _sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    codex_base = cs.DEFAULT_CHATGPT_SUBSCRIPTION_BASE_URL
    with factory() as db:
        db.add_all([
            database.Session(id="child-1", name="child", endpoint_url=codex_base, model="gpt-6-sol",
                             owner="robert", headers={}),
            database.Session(id="child-2", name="child", endpoint_url="https://api.openai.com/v1",
                             model="gpt-5", owner="robert", headers={}),
            database.ModelEndpoint(id="ep-other", name="Theirs", base_url=codex_base, is_enabled=True,
                                   owner="alice", provider_auth_id="auth-alice"),
            database.ModelEndpoint(id="ep-off", name="Old", base_url=codex_base, is_enabled=False,
                                   owner="robert", provider_auth_id="auth-old"),
            database.ModelEndpoint(id="ep-mine", name="Mine", base_url=codex_base, is_enabled=True,
                                   owner="robert", provider_auth_id="auth-robert"),
        ])
        db.commit()

    assert cs._session_credential_ref("child-1") == ("auth-robert", "robert")
    assert cs._session_credential_ref("child-2") is None  # not a ChatGPT chat
    assert cs._session_credential_ref("missing") is None
    engine.dispose()


# ── the metadata refresh that wiped the bearer ───────────────────────────


def _manager(monkeypatch, tmp_path):
    from sqlalchemy.orm import sessionmaker as _sessionmaker

    import core.database as database
    from core import session_manager as sm

    engine = create_engine(f"sqlite:///{tmp_path / 'sessions.db'}", connect_args={"check_same_thread": False})
    database.Base.metadata.create_all(engine, tables=[database.Session.__table__,
                                                      database.ChatMessage.__table__])
    monkeypatch.setattr(sm, "SessionLocal", _sessionmaker(bind=engine))
    manager = sm.SessionManager.__new__(sm.SessionManager)
    manager.sessions = {}
    return manager, engine


@pytest.mark.parametrize("endpoint,kept", [
    (cs.DEFAULT_CHATGPT_SUBSCRIPTION_BASE_URL, True),
    ("https://api.openai.com/v1", False),  # static keys live in the row: it stays authoritative
])
def test_get_session_keeps_a_request_local_chatgpt_bearer(monkeypatch, tmp_path, endpoint, kept):
    manager, engine = _manager(monkeypatch, tmp_path)
    sess = manager.create_session("child-1", "child", endpoint, "gpt-6-sol", owner="robert")
    sess.headers = cs.chatgpt_headers("tok-123")  # what resolve_session_auth does for a worker

    manager.get_session("child-1")  # what orchestrate_agents does right after launching

    assert ("Authorization" in sess.headers) is kept
    engine.dispose()


def test_get_session_drops_the_bearer_when_the_chat_moved_endpoint(monkeypatch, tmp_path):
    from core import session_manager as sm
    import core.database as database

    manager, engine = _manager(monkeypatch, tmp_path)
    sess = manager.create_session("child-1", "child", cs.DEFAULT_CHATGPT_SUBSCRIPTION_BASE_URL,
                                  "gpt-6-sol", owner="robert")
    sess.headers = cs.chatgpt_headers("tok-123")
    with sm.SessionLocal() as db:
        db.query(database.Session).filter(database.Session.id == "child-1").update(
            {"endpoint_url": "https://api.openai.com/v1"})
        db.commit()

    manager.get_session("child-1")

    assert sess.headers == {}
    engine.dispose()


# ── 502/503 before any output ────────────────────────────────────────────


class FlakyGateway(FakeCodex):
    """Answers the first ``failures`` requests with an Envoy-style 503."""

    def __init__(self, failures=1, status=503):
        super().__init__()
        self.failures, self.status = failures, status

    def stream(self, method, url, headers=None, **kwargs):
        ctx = super().stream(method, url, headers=headers, **kwargs)
        if len(self.bearers) > self.failures:
            return ctx
        status = self.status

        class _Resp:
            status_code = status

            async def aread(self):
                return (b"upstream connect error or disconnect/reset before headers. "
                        b"reset reason: connection termination")

        class _Ctx:
            async def __aenter__(self):
                return _Resp()

            async def __aexit__(self, *a):
                return False

        return _Ctx()


@pytest.fixture
def no_status_delay(monkeypatch):
    monkeypatch.setattr(llm_core, "_STREAM_STATUS_RETRY_DELAY", (0, 0))


@pytest.mark.parametrize("status", [502, 503, 504, 520, 524, 529])
async def test_gateway_error_before_output_is_replayed(store, codex, no_status_delay, caplog, status):
    caplog.set_level(logging.WARNING)
    fake = codex(FlakyGateway(failures=1, status=status))

    chunks = await _stream(store.initial_access)

    assert _errors(chunks) == []
    assert any('"delta": "hello"' in c for c in chunks)
    assert len(fake.bearers) == 2
    assert f"HTTP {status} from" in caplog.text and "retrying (1/3)" in caplog.text


async def test_an_edge_outage_lasting_three_tries_still_recovers(store, codex, no_status_delay):
    """2026-10-02: one HTTP 520 failed a worker mid-run."""
    fake = codex(FlakyGateway(failures=3, status=520))

    chunks = await _stream(store.initial_access)

    assert _errors(chunks) == []
    assert len(fake.bearers) == 4


async def test_gateway_error_after_three_replays_is_reported(store, codex, no_status_delay):
    fake = codex(FlakyGateway(failures=5))

    [error] = _errors(await _stream(store.initial_access))

    assert error["status"] == 503 and "upstream connect error" in error["raw"]
    assert len(fake.bearers) == 4


def test_status_retry_classification():
    def chunk(**data):
        return f"event: error\ndata: {json.dumps(data)}\n\n"

    assert llm_core._is_retryable_upstream_status_chunk(chunk(status=503, text="upstream connect error"))
    assert llm_core._is_retryable_upstream_status_chunk(chunk(status=502, text="Bad gateway"))
    # Its own budget, not this one.
    assert not llm_core._is_retryable_upstream_status_chunk(chunk(status=503, retryable=True, error="Cannot reach"))
    # Never reached the upstream.
    assert not llm_core._is_retryable_upstream_status_chunk(
        chunk(status=503, error="Upstream chatgpt.com unreachable (cooldown active)"))
    # Local transport failures the stream code already chose not to replay.
    assert not llm_core._is_retryable_upstream_status_chunk(
        chunk(status=502, error="Network error", fallback_eligible=False))
    assert llm_core._is_retryable_upstream_status_chunk(chunk(status=520, text="outage"))
    for status in (400, 401, 429, 500):
        assert not llm_core._is_retryable_upstream_status_chunk(chunk(status=status, text="x"))
    assert not llm_core._is_retryable_upstream_status_chunk('data: {"delta": "503"}\n\n')
