"""The document owner filter, run on a real query over real rows.

A document with no owner must never be visible to a logged-in user: the pattern
`owner == user or owner is None` has come back more than once.
"""
import pytest

from core.database import Document
from routes.document.document_helpers import _owner_session_filter

pytestmark = pytest.mark.security


@pytest.fixture
def documents(app_db):
    db = app_db.SessionLocal()
    for owner in ("alice", "bob", None):
        db.add(Document(id=f"doc-{owner}", title=f"{owner}'s", current_content="", owner=owner))
    db.commit()
    yield db
    db.close()


def _ids(db, user):
    return sorted(d.id for d in _owner_session_filter(db.query(Document), user))


def test_a_user_sees_only_their_own_documents(documents, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")

    assert _ids(documents, "alice") == ["doc-alice"]


def test_a_missing_user_sees_nothing_when_auth_is_on(documents, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")

    assert _ids(documents, None) == []


def test_single_user_mode_sees_every_document(documents, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")

    assert _ids(documents, None) == ["doc-None", "doc-alice", "doc-bob"]
