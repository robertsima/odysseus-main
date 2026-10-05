"""Document session owner-scope regressions.

Route handlers are called directly, matching the pattern used by the existing
document route tests. This keeps coverage on the real closures without spinning
up middleware.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from tests.helpers.import_state import clear_fake_database_modules

pytestmark = pytest.mark.security

clear_fake_database_modules()

import routes.document_routes as droutes
from core.database import Document
from core.database import Session as DbSession
from routes.document_helpers import DocumentPatch
from routes.document_helpers import _owner_session_filter

def _req(user="alice"):
    return SimpleNamespace(state=SimpleNamespace(current_user=user))


def _endpoint(method, path):
    router = droutes.setup_document_routes(MagicMock(), None)
    for route in router.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise RuntimeError(f"{method} {path} not found")


def _bind_test_db(app_db):
    previous = droutes.SessionLocal
    droutes.SessionLocal = app_db.SessionLocal
    return previous


def _seed(app_db):
    alice_session = "alice-" + uuid.uuid4().hex[:8]
    bob_session = "bob-" + uuid.uuid4().hex[:8]
    alice_doc = str(uuid.uuid4())
    bob_doc = str(uuid.uuid4())
    legacy_doc = str(uuid.uuid4())
    db = app_db.SessionLocal()
    try:
        db.add(DbSession(id=alice_session, owner="alice", name="alice", model="m", endpoint_url="http://x"))
        db.add(DbSession(id=bob_session, owner="bob", name="bob", model="m", endpoint_url="http://x"))
        db.add(Document(
            id=alice_doc,
            session_id=alice_session,
            title="alice doc",
            language="markdown",
            current_content="alice body",
            version_count=1,
            is_active=True,
            owner="alice",
        ))
        db.add(Document(
            id=bob_doc,
            session_id=bob_session,
            title="bob doc",
            language="markdown",
            current_content="bob body",
            version_count=1,
            is_active=True,
            owner="bob",
        ))
        db.add(Document(
            id=legacy_doc,
            session_id=alice_session,
            title="legacy doc",
            language="markdown",
            current_content="legacy body",
            version_count=1,
            is_active=True,
            owner=None,
        ))
        db.commit()
        return alice_session, bob_session, alice_doc, bob_doc, legacy_doc
    finally:
        db.close()


@pytest.mark.asyncio
async def test_patch_document_rejects_cross_owner_session_link(app_db):
    previous_session_local = _bind_test_db(app_db)
    try:
        patch_document = _endpoint("PATCH", "/api/document/{doc_id}")
        alice_session, bob_session, _alice_doc, bob_doc, _legacy_doc = _seed(app_db)

        with pytest.raises(HTTPException) as exc:
            await patch_document(_req("bob"), bob_doc, DocumentPatch(session_id=alice_session))

        assert exc.value.status_code == 404
        db = app_db.SessionLocal()
        try:
            assert db.query(Document).filter(Document.id == bob_doc).first().session_id == bob_session
        finally:
            db.close()
    finally:
        droutes.SessionLocal = previous_session_local


@pytest.mark.asyncio
async def test_list_documents_filters_foreign_docs_in_visible_session(app_db):
    previous_session_local = _bind_test_db(app_db)
    try:
        list_documents = _endpoint("GET", "/api/documents/{session_id}")
        alice_session, _bob_session, alice_doc, bob_doc, legacy_doc = _seed(app_db)
        db = app_db.SessionLocal()
        try:
            db.query(Document).filter(Document.id == bob_doc).update({"session_id": alice_session})
            db.commit()
        finally:
            db.close()

        rows = await list_documents(_req("alice"), alice_session)
        ids = {row["id"] for row in rows}

        assert alice_doc in ids
        assert legacy_doc in ids
        assert bob_doc not in ids
    finally:
        droutes.SessionLocal = previous_session_local


def test_owner_session_filter_noops_for_auth_disabled_single_user(monkeypatch, app_db):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    previous_session_local = _bind_test_db(app_db)
    try:
        _alice_session, _bob_session, alice_doc, _bob_doc, _legacy_doc = _seed(app_db)
        db = app_db.SessionLocal()
        try:
            q = db.query(Document).filter(Document.id == alice_doc)
            assert _owner_session_filter(q, None).first().id == alice_doc
        finally:
            db.close()
    finally:
        droutes.SessionLocal = previous_session_local
