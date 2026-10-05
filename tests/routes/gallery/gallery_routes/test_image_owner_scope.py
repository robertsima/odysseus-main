"""A user reaches only their own gallery images.

Bob's request for one of Alice's images must change nothing, and should get
the answer a made-up image id gets, so the response can't confirm the id.
"""
import hashlib
import uuid

import pytest

from tests.routes._support.images import FakeModelServer, png_bytes

pytestmark = pytest.mark.security

ALICE_PNG = png_bytes(size=(4, 2), color=(10, 120, 200))


@pytest.fixture
def alice(api):
    return api.as_user("alice")


@pytest.fixture
def bob(api):
    return api.as_user("bob")


def _upload(client, name, data):
    response = client.post("/api/gallery/upload", files={"file": (name, data, "image/png")})
    assert response.status_code == 200 and response.json()["ok"], response.text
    return response.json()


@pytest.fixture
def alice_image(alice):
    return _upload(alice, "beach.png", ALICE_PNG)


def _file_hash(filename):
    from routes.gallery import gallery_routes

    return hashlib.sha256((gallery_routes.GALLERY_IMAGE_DIR / filename).read_bytes()).hexdigest()


def _alice_view(alice, image):
    row = alice.get(f"/api/gallery/{image['id']}").json()
    row.pop("updated_at", None)
    return row, _file_hash(image["filename"])


def _replacement():
    return {"files": {"image": ("edited.png", png_bytes(size=(8, 8), color=(0, 0, 0)), "image/png")}}


REFUSED_AS_MISSING = [
    ("GET", "/api/gallery/{id}", {}),
    ("PATCH", "/api/gallery/{id}", {"json": {"tags": "bob", "favorite": True}}),
    ("DELETE", "/api/gallery/{id}", {}),
    ("POST", "/api/gallery/{id}/favorite", {}),
    ("POST", "/api/gallery/{id}/ai-tag", {}),
]

# These three answer "403 Not your image" for someone else's image and 404 for
# an unknown id.
REFUSED_AS_FORBIDDEN = [
    ("POST", "/api/gallery/{id}/rename", {"json": {"name": "bob's photo"}}),
    ("POST", "/api/gallery/{id}/rotate", {"json": {"angle": 90}}),
    ("POST", "/api/gallery/{id}/replace", "replacement"),
]


def _kwargs(spec):
    return _replacement() if spec == "replacement" else spec


@pytest.mark.parametrize("method, path, kwargs", REFUSED_AS_MISSING)
def test_another_users_image_answers_like_a_missing_one(bob, alice_image, method, path, kwargs):
    on_alices = bob.request(method, path.format(id=alice_image["id"]), **kwargs)
    on_missing = bob.request(method, path.format(id=str(uuid.uuid4())), **kwargs)

    assert on_alices.status_code == 404
    assert on_alices.json() == on_missing.json()


@pytest.mark.parametrize("method, path, kwargs", REFUSED_AS_FORBIDDEN)
@pytest.mark.xfail(strict=True, reason=(
    "security bug: rename, rotate and replace answer 403 'Not your image' for another user's "
    "image but 404 for an unknown id, so the status confirms that the image id exists"
))
def test_rename_rotate_and_replace_answer_like_a_missing_image(bob, alice_image, method, path, kwargs):
    on_alices = bob.request(method, path.format(id=alice_image["id"]), **_kwargs(kwargs))
    on_missing = bob.request(method, path.format(id=str(uuid.uuid4())), **_kwargs(kwargs))

    assert (on_alices.status_code, on_alices.json()) == (on_missing.status_code, on_missing.json())


@pytest.mark.parametrize("method, path, kwargs", REFUSED_AS_MISSING[1:] + REFUSED_AS_FORBIDDEN)
def test_another_user_cannot_change_or_delete_an_image(alice, bob, alice_image, method, path, kwargs):
    before = _alice_view(alice, alice_image)

    response = bob.request(method, path.format(id=alice_image["id"]), **_kwargs(kwargs))

    assert response.status_code in (403, 404)
    assert _alice_view(alice, alice_image) == before


def test_the_owner_can_tag_favorite_rename_and_rotate_their_image(alice, alice_image):
    image_id = alice_image["id"]

    patched = alice.patch(f"/api/gallery/{image_id}", json={"tags": "sea, sand"}).json()
    assert patched["tags"] == "sea, sand"
    assert alice.post(f"/api/gallery/{image_id}/favorite").json() == {"ok": True, "favorite": True}
    assert alice.post(f"/api/gallery/{image_id}/rename", json={"name": "Dune"}).json() == {"ok": True, "name": "Dune"}

    rotated = alice.post(f"/api/gallery/{image_id}/rotate", json={"angle": 90}).json()
    assert (rotated["width"], rotated["height"]) == (2, 4)


def test_the_owner_can_replace_and_delete_their_image(alice, alice_image):
    image_id = alice_image["id"]

    replaced = alice.post(f"/api/gallery/{image_id}/replace", **_replacement()).json()
    assert (replaced["width"], replaced["height"]) == (8, 8)

    assert alice.delete(f"/api/gallery/{image_id}").json() == {"status": "deleted", "id": image_id}
    assert alice.get("/api/gallery/library").json()["items"] == []


def test_the_owner_can_ai_tag_their_image(monkeypatch, alice, alice_image):
    import src.document_processor

    server = FakeModelServer({"choices": [{"message": {"content": "Beach, Sunset"}}]}).install(monkeypatch)
    monkeypatch.setattr(src.document_processor, "_load_vl_settings", lambda: {"vision_enabled": True})
    monkeypatch.setattr(
        src.document_processor, "_resolve_vl_model",
        lambda configured, owner=None: ("http://127.0.0.1:9/v1/chat/completions", "vision-model", {}),
    )

    result = alice.post(f"/api/gallery/{alice_image['id']}/ai-tag").json()

    assert result == {"ok": True, "ai_tags": "beach, sunset"}
    assert len(server.calls) == 1
