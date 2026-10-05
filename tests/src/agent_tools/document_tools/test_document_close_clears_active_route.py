"""Issue #1160 — route-level regression for clearing the active-document pointer.

Exercises the REAL ``PATCH /api/document/{id}`` (session_id="") and
``DELETE /api/document/{id}`` handlers, proving that closing a document's tab
(detach or delete) clears the in-memory active-document pointer under the actual
owner/session routing — not just the helper in isolation.

Calls the route handler callables DIRECTLY (extracted from the router) instead of
through Starlette's TestClient. The TestClient path spun up a middleware app +
threadpool that could hang in some environments; calling the async handler with a
minimal fake request keeps the same real coverage (handler + DB + owner routing)
while completing reliably everywhere.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

import routes.document_routes as droutes
from core.database import Document
from core.database import Session as DbSession
from routes.document_helpers import DocumentPatch
from src.agent_tools.document_tools import set_active_document, get_active_document


@pytest.fixture(autouse=True)
def _bind_test_db(monkeypatch, app_db):
    # Route handlers resolve SessionLocal at call time.
    monkeypatch.setattr(droutes, "SessionLocal", app_db.SessionLocal)


def _req():
    return SimpleNamespace(state=SimpleNamespace(current_user="tester"))


def _endpoint(method, path):
    router = droutes.setup_document_routes(MagicMock(), None)
    for r in router.routes:
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set()):
            return r.endpoint
    raise RuntimeError(f"{method} {path} not found")


def _make_doc(app_db):
    sid = "s-" + uuid.uuid4().hex[:8]
    db = app_db.SessionLocal()
    try:
        db.add(DbSession(id=sid, owner="tester", name="s", model="m", endpoint_url="http://x"))
        doc = Document(
            id=str(uuid.uuid4()), session_id=sid, title="t",
            language="markdown", current_content="hi", version_count=1,
            is_active=True, owner="tester",
        )
        db.add(doc)
        db.commit()
        return doc.id
    finally:
        db.close()


async def test_patch_unlink_clears_active_document(app_db):
    patch_document = _endpoint("PATCH", "/api/document/{doc_id}")
    doc_id = _make_doc(app_db)
    set_active_document(doc_id)
    await patch_document(_req(), doc_id, DocumentPatch(session_id=""))
    assert get_active_document() is None


async def test_delete_clears_active_document(app_db):
    delete_document = _endpoint("DELETE", "/api/document/{doc_id}")
    doc_id = _make_doc(app_db)
    set_active_document(doc_id)
    await delete_document(_req(), doc_id)
    assert get_active_document() is None


async def test_unlinking_a_different_doc_leaves_pointer(app_db):
    patch_document = _endpoint("PATCH", "/api/document/{doc_id}")
    active_id = _make_doc(app_db)
    other_id = _make_doc(app_db)
    set_active_document(active_id)
    await patch_document(_req(), other_id, DocumentPatch(session_id=""))
    assert get_active_document() == active_id
