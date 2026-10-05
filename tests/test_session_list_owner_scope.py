"""list_sessions must return only the authenticated user's sessions.

Regression for the enrichment query at routes/session_routes.py:265 which
previously fetched rows for all owners on every GET /api/sessions call.
"""
import sys
import tempfile
import types
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import ChatMessage as DbMessage
from core.database import Session as DbSession

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)


def _stub_multipart_if_missing(monkeypatch):
    try:
        import python_multipart  # noqa: F401
        return
    except ImportError:
        pass
    stub = types.ModuleType("python_multipart")
    stub.__version__ = "0.0.20"
    monkeypatch.setitem(sys.modules, "python_multipart", stub)


def _fresh_router(monkeypatch, sr):
    """Give setup_session_routes an empty router for this test.

    It adds its routes to the module-level router that app.py includes, so a
    MagicMock session manager registered here would answer /api/session in
    every app built later in the process.
    """
    from fastapi import APIRouter

    monkeypatch.setattr(sr, "router", APIRouter(prefix="/api"))


def test_list_sessions_excludes_other_users_sessions(monkeypatch):
    import routes.session_routes as sr
    from unittest.mock import MagicMock

    _stub_multipart_if_missing(monkeypatch)
    monkeypatch.setattr(sr, "SessionLocal", _TS)
    monkeypatch.setattr(sr, "effective_user", lambda request: "alice")

    alice_id = str(uuid.uuid4())
    bob_id = str(uuid.uuid4())
    db = _TS()
    try:
        db.query(DbSession).delete()
        db.add(DbSession(id=alice_id, owner="alice", name="alice session",
                         endpoint_url="http://localhost", model="gpt-4", archived=False))
        db.add(DbSession(id=bob_id, owner="bob", name="bob session",
                         endpoint_url="http://localhost", model="gpt-4", archived=False))
        db.commit()
    finally:
        db.close()

    alice_session = MagicMock(id=alice_id, name="alice session",
                              model="gpt-4", endpoint_url="http://localhost",
                              rag=False, archived=False)
    sm = MagicMock()
    sm.get_sessions_for_user.return_value = {alice_id: alice_session}
    _fresh_router(monkeypatch, sr)
    router = sr.setup_session_routes(sm, {})
    # The router is module-level and every setup call appends its routes, so
    # an earlier test in the same process leaves an /api/sessions bound to its
    # own session manager: take the one this call registered (the last), or
    # the test sees that manager's chats instead (intermittent under xdist).
    endpoint = [r.endpoint for r in router.routes
                if getattr(r, "path", "") == "/api/sessions"
                and "GET" in getattr(r, "methods", set())][-1]

    result = endpoint(request=MagicMock())
    returned_ids = {s["id"] for s in result}
    assert alice_id in returned_ids
    assert bob_id not in returned_ids


def test_auto_sort_skip_llm_cleans_owner_stamped_sessions_when_auth_disabled(monkeypatch):
    import routes.session_routes as sr
    from unittest.mock import MagicMock

    _stub_multipart_if_missing(monkeypatch)
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(sr, "SessionLocal", _TS)
    monkeypatch.setattr(sr, "effective_user", lambda request: None)

    sid = str(uuid.uuid4())
    old_time = cdb.utcnow_naive() - timedelta(hours=2)
    db = _TS()
    try:
        db.query(DbMessage).delete()
        db.query(DbSession).delete()
        db.add(DbSession(
            id=sid,
            owner="alice",
            name="New chat",
            endpoint_url="http://localhost",
            model="gpt-4",
            archived=False,
            message_count=1,
            created_at=old_time,
            updated_at=old_time,
            last_message_at=old_time,
            last_accessed=old_time,
        ))
        db.add(DbMessage(
            id="m-" + uuid.uuid4().hex,
            session_id=sid,
            role="user",
            content="hi",
            timestamp=old_time,
        ))
        db.commit()
    finally:
        db.close()

    session = MagicMock(id=sid, name="New chat", model="gpt-4", endpoint_url="http://localhost", rag=False, archived=False)
    sm = MagicMock()
    sm.get_sessions_for_user.return_value = {sid: session}
    _fresh_router(monkeypatch, sr)
    router = sr.setup_session_routes(sm, {})
    endpoint = next(r.endpoint for r in router.routes
                    if getattr(r, "path", "") == "/api/sessions/auto-sort"
                    and "POST" in getattr(r, "methods", set()))
    assert any(r.endpoint is endpoint and r.path == "/api/chats/tidy" and "POST" in r.methods
               for r in router.routes)

    result = endpoint(request=MagicMock(), skip_llm=True)

    assert result["deleted_throwaway"] == 1
    db = _TS()
    try:
        assert db.query(DbSession).filter(DbSession.id == sid).first() is None
    finally:
        db.close()


def test_library_chat_tidy_hits_real_registered_api_route(monkeypatch, tmp_path):
    """The Library's origin + /api/chats/tidy reaches the real ASGI handler.

    Without the alias this POST returned 404, though auto-sort existed.
    An empty session manager keeps the test independent of an external LLM.
    """
    from fastapi import FastAPI, APIRouter, Depends
    from fastapi.testclient import TestClient
    from unittest.mock import MagicMock
    import routes.session_routes as sr
    from src.auth_helpers import require_chat_api_token_scope

    _stub_multipart_if_missing(monkeypatch)
    # setup_session_routes adds closures to a module-level router. Earlier
    # tests' handlers must not capture this test's request instead.
    monkeypatch.setattr(sr, 'router', APIRouter(prefix='/api', dependencies=[Depends(require_chat_api_token_scope)]))
    engine = create_engine(f"sqlite:///{tmp_path / 'tidy.db'}", connect_args={"check_same_thread": False})
    cdb.Base.metadata.create_all(engine)
    monkeypatch.setattr(sr, "SessionLocal", sessionmaker(bind=engine))
    session_manager = MagicMock()
    session_manager.get_sessions_for_user.return_value = {}
    app = FastAPI()

    @app.middleware('http')
    async def cookie_identity(request, call_next):
        request.state.current_user = 'alice'
        return await call_next(request)

    app.include_router(sr.setup_session_routes(session_manager, {}))
    client = TestClient(app, base_url="http://localhost:8000")
    response = client.post('/api/chats/tidy')
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'skipped'
    assert client.post('/api/sessions/auto-sort?skip_llm=true').status_code == 200
    assert client.post('/chats/tidy?skip_llm=true').status_code == 404
    session_manager.get_sessions_for_user.assert_called_with('alice')
