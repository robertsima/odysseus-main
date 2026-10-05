"""resolve_session_auth must not persist the ChatGPT Subscription bearer.

The ChatGPT Subscription access token is a short-lived OAuth bearer re-resolved
(and refreshed) on every request. resolve_session_auth() may set it on the
in-memory session for the current request, but it must never write it back into
the sessions table — otherwise the live token sits at rest as
"Authorization: Bearer ...". Only the encrypted refresh token in
ProviderAuthSession is allowed to persist.
"""

import types

import routes.chat_helpers as chat_helpers
import src.endpoint_resolver as endpoint_resolver
from core.database import ModelEndpoint, Session as DbSession
import pytest

pytestmark = pytest.mark.security

_CODEX_BASE = "https://chatgpt.com/backend-api/codex"


def _mem_db(monkeypatch, make_test_db):
    # make_test_db's SessionLocal matches production SessionLocal
    # (core.database), which is autoflush=False.
    TestSessionLocal = make_test_db(memory=True).SessionLocal
    monkeypatch.setattr(chat_helpers, "SessionLocal", TestSessionLocal)
    return TestSessionLocal


def test_chatgpt_subscription_auth_is_not_written_to_sessions_table(monkeypatch, make_test_db):
    TestSessionLocal = _mem_db(monkeypatch, make_test_db)
    db = TestSessionLocal()
    try:
        db.add(ModelEndpoint(
            id="ep1", name="ChatGPT Subscription", base_url=_CODEX_BASE,
            provider_auth_id="auth1", owner="alice", is_enabled=True, api_key=None,
        ))
        db.add(DbSession(
            id="sess1", name="chat", endpoint_url=_CODEX_BASE,
            model="gpt-5.1-codex", owner="alice", headers={},
        ))
        db.commit()
    finally:
        db.close()

    # A live access token is resolved at request time.
    monkeypatch.setattr(
        endpoint_resolver, "resolve_endpoint_runtime",
        lambda ep, owner=None: (_CODEX_BASE, "live-access-token"),
    )

    sess = types.SimpleNamespace(
        id="sess1", endpoint_url=_CODEX_BASE, model="gpt-5.1-codex",
        owner="alice", headers={},
    )
    chat_helpers.resolve_session_auth(sess, "sess1", owner="alice")

    # In-memory session got request-local auth for this request...
    assert any(k.lower() == "authorization" for k in sess.headers)
    assert sess.headers["Authorization"] == "Bearer live-access-token"

    # ...but the DB row must NOT have the bearer persisted.
    db = TestSessionLocal()
    try:
        row = db.query(DbSession).filter(DbSession.id == "sess1").first()
        stored = row.headers or {}
        assert not any(k.lower() == "authorization" for k in stored), (
            f"ChatGPT bearer leaked into sessions table: {stored}"
        )
    finally:
        db.close()


def test_non_subscription_auth_is_still_persisted_to_sessions_table(monkeypatch, make_test_db):
    """The early-return must be scoped to ChatGPT Subscription only.

    Ordinary endpoints rely on resolve_session_auth() persisting the resolved
    headers into the sessions table so they aren't re-resolved on every request.
    If the is_chatgpt_subscription guard ever widened, this would silently break;
    this test pins the persistence path as still reached for normal endpoints.
    """
    base = "https://api.example.com/v1"
    TestSessionLocal = _mem_db(monkeypatch, make_test_db)
    db = TestSessionLocal()
    try:
        db.add(ModelEndpoint(
            id="ep1", name="Generic", base_url=base,
            owner="alice", is_enabled=True, api_key="sk-static",
        ))
        db.add(DbSession(
            id="sess1", name="chat", endpoint_url=base,
            model="gpt-x", owner="alice", headers={},
        ))
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(
        endpoint_resolver, "resolve_endpoint_runtime",
        lambda ep, owner=None: (base, "sk-static"),
    )

    sess = types.SimpleNamespace(
        id="sess1", endpoint_url=base, model="gpt-x", owner="alice", headers={},
    )
    chat_helpers.resolve_session_auth(sess, "sess1", owner="alice")

    # In-memory session got auth...
    assert any(k.lower() in ("authorization", "x-api-key") for k in sess.headers)

    # ...AND it was persisted to the DB row (the normal, non-subscription path).
    db = TestSessionLocal()
    try:
        row = db.query(DbSession).filter(DbSession.id == "sess1").first()
        stored = row.headers or {}
        assert any(k.lower() in ("authorization", "x-api-key") for k in stored), (
            f"non-subscription auth was not persisted: {stored}"
        )
    finally:
        db.close()


def test_chatgpt_subscription_clears_previously_persisted_bearer(monkeypatch, make_test_db):
    """A bearer left at rest by an older code path is stripped on next resolve."""
    TestSessionLocal = _mem_db(monkeypatch, make_test_db)
    db = TestSessionLocal()
    try:
        db.add(ModelEndpoint(
            id="ep1", name="ChatGPT Subscription", base_url=_CODEX_BASE,
            provider_auth_id="auth1", owner="alice", is_enabled=True, api_key=None,
        ))
        # Simulate the leak: a stale bearer already sitting in the sessions table.
        db.add(DbSession(
            id="sess1", name="chat", endpoint_url=_CODEX_BASE,
            model="gpt-5.1-codex", owner="alice",
            headers={"Authorization": "Bearer stale-leaked-token"},
        ))
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(
        endpoint_resolver,
        "resolve_endpoint_runtime",
        lambda ep, owner=None: (_CODEX_BASE, "live-access-token"),
    )

    sess = types.SimpleNamespace(
        id="sess1", endpoint_url=_CODEX_BASE, model="gpt-5.1-codex",
        owner="alice", headers={},
    )
    chat_helpers.resolve_session_auth(sess, "sess1", owner="alice")

    # The stale bearer must have been stripped from the DB row.
    db = TestSessionLocal()
    try:
        row = db.query(DbSession).filter(DbSession.id == "sess1").first()
        stored = row.headers or {}
        assert not any(k.lower() == "authorization" for k in stored), (
            f"stale ChatGPT bearer was not cleared: {stored}"
        )
    finally:
        db.close()
