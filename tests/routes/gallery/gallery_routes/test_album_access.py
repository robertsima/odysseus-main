"""A user reaches only their own albums, and can file only their own images.

Another user's album id must answer like a made-up one, and no request may
move an image into, out of, or onto the cover of an album that belongs to
someone else.
"""
import uuid

import pytest

from tests.routes._support.images import png_bytes

pytestmark = pytest.mark.security


@pytest.fixture
def alice(api):
    return api.as_user("alice")


@pytest.fixture
def bob(api):
    return api.as_user("bob")


def _upload(client, name, color, **form):
    response = client.post(
        "/api/gallery/upload",
        files={"file": (name, png_bytes(color=color), "image/png")},
        data=form,
    )
    return response


def _image(client, name, color):
    response = _upload(client, name, color)
    assert response.status_code == 200 and response.json()["ok"], response.text
    return response.json()["id"]


def _album(client, name):
    response = client.post("/api/gallery/albums", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["id"]


@pytest.fixture
def alice_album(alice):
    album = _album(alice, "Honeymoon")
    image = _image(alice, "kiss.png", (250, 0, 0))
    assert alice.post(f"/api/gallery/albums/{album}/add", json={"image_ids": [image]}).json()["ok"]
    return {"id": album, "image": image}


def _alice_album_view(alice, album_id):
    albums = {a["id"]: a for a in alice.get("/api/gallery/albums").json()["albums"]}
    items = alice.get("/api/gallery/library", params={"album": album_id}).json()["items"]
    album = albums.get(album_id, {})
    return album.get("name"), album.get("count"), album.get("cover_url"), sorted(i["id"] for i in items)


ALBUM_CALLS = [
    ("PUT", "/api/gallery/albums/{id}", {"name": "bob's album"}),
    ("DELETE", "/api/gallery/albums/{id}", None),
    ("POST", "/api/gallery/albums/{id}/add", {"image_ids": []}),
    ("POST", "/api/gallery/albums/{id}/remove", {"image_ids": []}),
]


@pytest.mark.parametrize("method, path, body", ALBUM_CALLS)
def test_another_users_album_answers_like_a_missing_one(bob, alice_album, method, path, body):
    on_alices = bob.request(method, path.format(id=alice_album["id"]), json=body)
    on_missing = bob.request(method, path.format(id=str(uuid.uuid4())), json=body)

    assert on_alices.status_code == 404
    assert on_alices.json() == on_missing.json()


def test_another_user_cannot_rename_delete_or_empty_an_album(alice, bob, alice_album):
    before = _alice_album_view(alice, alice_album["id"])
    album = alice_album["id"]

    bob.put(f"/api/gallery/albums/{album}", json={"name": "bob's album"})
    bob.post(f"/api/gallery/albums/{album}/remove", json={"image_ids": [alice_album["image"]]})
    bob.delete(f"/api/gallery/albums/{album}")

    assert _alice_album_view(alice, album) == before


def test_another_user_cannot_file_images_into_an_album(alice, bob, alice_album):
    before = _alice_album_view(alice, alice_album["id"])
    bob_image = _image(bob, "meme.png", (0, 250, 0))

    added = bob.post(f"/api/gallery/albums/{alice_album['id']}/add", json={"image_ids": [bob_image]})
    patched = bob.patch(f"/api/gallery/{bob_image}", json={"album_id": alice_album["id"]})
    uploaded = _upload(bob, "spam.png", (0, 0, 250), album_id=alice_album["id"])

    assert (added.status_code, patched.status_code, uploaded.status_code) == (404, 404, 404)
    assert _alice_album_view(alice, alice_album["id"]) == before


def test_moving_images_into_an_album_moves_only_the_callers_images(alice, bob, alice_album):
    bob_album = _album(bob, "Bob's stuff")

    bob.post(f"/api/gallery/albums/{bob_album}/add", json={"image_ids": [alice_album["image"]]})

    assert alice.get(f"/api/gallery/{alice_album['image']}").json()["album_id"] == alice_album["id"]
    assert bob.get("/api/gallery/library", params={"album": bob_album}).json()["items"] == []


def test_another_users_image_cannot_become_an_album_cover(bob, alice_album):
    bob_album = _album(bob, "Bob's stuff")

    response = bob.put(f"/api/gallery/albums/{bob_album}", json={"cover_id": alice_album["image"]})

    assert response.status_code == 404
    cover = {a["id"]: a for a in bob.get("/api/gallery/albums").json()["albums"]}[bob_album]["cover_url"]
    assert cover is None


def test_the_album_list_shows_only_the_callers_albums(bob, alice_album):
    assert bob.get("/api/gallery/albums").json() == {"albums": []}


def test_the_owner_can_manage_their_album(alice, alice_album):
    album, image = alice_album["id"], alice_album["image"]

    assert alice.put(f"/api/gallery/albums/{album}", json={"name": "Paris", "cover_id": image}).json() == {"ok": True}
    name, count, cover, items = _alice_album_view(alice, album)
    assert (name, count, items) == ("Paris", 1, [image])
    assert cover and cover.startswith("/api/generated-image/")

    assert alice.post(f"/api/gallery/albums/{album}/remove", json={"image_ids": [image]}).json() == {"ok": True}
    assert alice.get(f"/api/gallery/{image}").json()["album_id"] is None

    assert alice.delete(f"/api/gallery/albums/{album}").json() == {"ok": True}
    assert alice.get("/api/gallery/albums").json() == {"albums": []}
