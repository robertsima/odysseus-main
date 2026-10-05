"""The gallery owner filter, run on a real query over real rows.

A row with no owner must never be visible to a logged-in user: the pattern
`owner == user or owner is None` has come back more than once.
"""
import pytest

from core.database import GalleryImage
from routes.gallery.gallery_helpers import _owner_filter

pytestmark = pytest.mark.security


@pytest.fixture
def images(app_db):
    db = app_db.SessionLocal()
    for owner in ("alice", "bob", None):
        db.add(GalleryImage(id=f"img-{owner}", filename=f"{owner}.png", owner=owner))
    db.commit()
    yield db
    db.close()


def _ids(db, user):
    return sorted(i.id for i in _owner_filter(db.query(GalleryImage), user))


def test_a_user_sees_only_their_own_images(images, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")

    assert _ids(images, "alice") == ["img-alice"]


def test_a_missing_user_sees_nothing_when_auth_is_on(images, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")

    assert _ids(images, None) == []


def test_single_user_mode_sees_every_image(images, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")

    assert _ids(images, None) == ["img-None", "img-alice", "img-bob"]
