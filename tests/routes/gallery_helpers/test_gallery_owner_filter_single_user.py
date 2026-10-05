"""_owner_filter must separate single-user mode from anonymous callers.

When AUTH_ENABLED=false, get_current_user returns None and gallery routes should
stay all-visible. When AUTH_ENABLED=true and no current user resolves, the same
None means an anonymous caller and gallery queries must fail closed.
"""
import uuid

import pytest

from core.database import GalleryImage
from routes.gallery_helpers import _owner_filter

pytestmark = pytest.mark.security


def _seed(app_db, *owners):
    db = app_db.SessionLocal()
    try:
        for o in owners:
            db.add(GalleryImage(id=str(uuid.uuid4()), filename=f"{uuid.uuid4().hex}.png", owner=o))
        db.commit()
    finally:
        db.close()


def test_none_user_returns_all_rows(monkeypatch, app_db):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    _seed(app_db, None, None, "alice")
    db = app_db.SessionLocal()
    try:
        n = _owner_filter(db.query(GalleryImage), None).count()
        assert n == 3  # old code returned 0
    finally:
        db.close()


def test_named_user_is_still_scoped(app_db):
    _seed(app_db, "alice", "alice", "bob", None)
    db = app_db.SessionLocal()
    try:
        assert _owner_filter(db.query(GalleryImage), "alice").count() == 2
        assert _owner_filter(db.query(GalleryImage), "bob").count() == 1
    finally:
        db.close()


def test_none_user_blocks_when_auth_is_enabled(monkeypatch, app_db):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    _seed(app_db, None, "alice", "bob")
    db = app_db.SessionLocal()
    try:
        assert _owner_filter(db.query(GalleryImage), None).count() == 0
    finally:
        db.close()
