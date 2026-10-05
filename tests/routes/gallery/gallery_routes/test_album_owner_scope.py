"""Gallery albums belong to one user (issue #2754).

Alice and bob each have an album with one image in it. One more image of
bob's points at alice's album, the kind of row a cross-user move used to
leave behind; it is the newest image, so it would be the album's cover.
"""
import base64
from datetime import datetime, timedelta

import pytest

import src.database
from core.database import GalleryAlbum, GalleryImage

pytestmark = pytest.mark.security

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@pytest.fixture
def gallery(api):
    now = datetime(2026, 10, 1)
    db = src.database.SessionLocal()
    try:
        db.add_all([
            GalleryAlbum(id="album-alice", name="Alice album", owner="alice"),
            GalleryAlbum(id="album-bob", name="Bob album", owner="bob"),
        ])
        for image_id, owner, album, minutes in (
            ("img-alice", "alice", "album-alice", 0),
            ("img-bob", "bob", "album-bob", 1),
            ("img-bob-stray", "bob", "album-alice", 2),
        ):
            db.add(GalleryImage(
                id=image_id, filename=f"{image_id}.png", prompt="", model="m", owner=owner,
                album_id=album, is_active=True, file_size=10, created_at=now + timedelta(minutes=minutes),
            ))
        db.commit()
    finally:
        db.close()


def _album_of(image_id):
    db = src.database.SessionLocal()
    try:
        return db.query(GalleryImage).filter(GalleryImage.id == image_id).one().album_id
    finally:
        db.close()


def test_an_image_cannot_be_moved_into_another_users_album(api, gallery):
    response = api.as_user("bob").patch("/api/gallery/img-bob", json={"album_id": "album-alice"})

    assert response.status_code == 404
    assert _album_of("img-bob") == "album-bob"


def test_an_upload_cannot_land_in_another_users_album(api, gallery):
    response = api.as_user("bob").post(
        "/api/gallery/upload", files={"file": ("new.png", PNG, "image/png")}, data={"album_id": "album-alice"})

    assert response.status_code == 404
    alice_album = api.as_user("alice").get("/api/gallery/albums").json()["albums"]
    assert [album["count"] for album in alice_album] == [1]


def test_an_album_counts_and_covers_only_its_owners_images(api, gallery):
    [album] = api.as_user("alice").get("/api/gallery/albums").json()["albums"]

    assert album["count"] == 1
    assert album["cover_url"].endswith("/img-alice.png")


@pytest.mark.parametrize("method,path,body", [
    ("PUT", "/api/gallery/albums/album-alice", {"name": "taken over"}),
    ("DELETE", "/api/gallery/albums/album-alice", None),
    ("POST", "/api/gallery/albums/album-alice/add", {"image_ids": ["img-bob"]}),
    ("POST", "/api/gallery/albums/album-alice/remove", {"image_ids": ["img-alice"]}),
], ids=["rename", "delete", "add", "remove"])
def test_a_user_cannot_change_another_users_album(api, gallery, method, path, body):
    response = api.as_user("bob").request(method, path, json=body)

    assert response.status_code == 404
    [album] = api.as_user("alice").get("/api/gallery/albums").json()["albums"]
    assert album["name"] == "Alice album"
    assert _album_of("img-alice") == "album-alice"
    assert _album_of("img-bob") == "album-bob"
