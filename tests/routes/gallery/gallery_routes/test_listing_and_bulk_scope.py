"""Gallery listings, counts and bulk actions see only the caller's images."""
import io
import zipfile

import pytest

from tests.routes._support.images import png_bytes

pytestmark = pytest.mark.security

ALICE_PNG = png_bytes(color=(10, 120, 200))


@pytest.fixture
def alice(api):
    return api.as_user("alice")


@pytest.fixture
def bob(api):
    return api.as_user("bob")


def _upload(client, name, data):
    response = client.post("/api/gallery/upload", files={"file": (name, data, "image/png")})
    assert response.status_code == 200, response.text
    return response.json()


def _set_ai_tags(image_id, ai_tags):
    """AI tags come from a vision model run; seed them directly."""
    import core.database as database

    db = database.SessionLocal()
    try:
        db.query(database.GalleryImage).filter(database.GalleryImage.id == image_id).update({"ai_tags": ai_tags})
        db.commit()
    finally:
        db.close()


@pytest.fixture
def alice_image(alice):
    image = _upload(alice, "passport.png", ALICE_PNG)
    alice.patch(f"/api/gallery/{image['id']}", json={"tags": "passport, travel", "favorite": True})
    _set_ai_tags(image["id"], "document, face")
    return image


def test_the_library_and_its_filters_show_only_the_callers_images(alice, bob, alice_image):
    alice_lib = alice.get("/api/gallery/library").json()
    bob_lib = bob.get("/api/gallery/library").json()
    bob_search = bob.get("/api/gallery/library", params={"search": "passport"}).json()

    assert [i["id"] for i in alice_lib["items"]] == [alice_image["id"]]
    assert (bob_lib["items"], bob_lib["total"], bob_lib["tags"], bob_lib["models"]) == ([], 0, [], [])
    assert bob_search["total"] == 0


def test_tags_and_stats_count_only_the_callers_images(bob, alice_image):
    assert bob.get("/api/gallery/tags").json() == {"tags": []}

    stats = bob.get("/api/gallery/stats").json()
    assert (stats["total_photos"], stats["total_size"], stats["favorites"]) == (0, 0, 0)


def test_the_ai_tag_queue_lists_only_the_callers_images(alice, bob):
    alice_untagged = _upload(alice, "untagged.png", ALICE_PNG)["id"]

    bob_queue = bob.post("/api/gallery/ai-tag-batch").json()
    alice_queue = alice.post("/api/gallery/ai-tag-batch").json()

    assert (bob_queue["image_ids"], bob_queue["total_untagged"]) == ([], 0)
    assert alice_queue["image_ids"] == [alice_untagged]


def test_an_upload_is_not_called_a_duplicate_of_another_users_photo(bob, alice_image):
    response = _upload(bob, "same.png", ALICE_PNG)

    assert response["ok"] is True
    assert alice_image["id"] not in str(response) and alice_image["filename"] not in str(response)


def test_the_owner_is_told_about_their_own_duplicate(alice, alice_image):
    response = _upload(alice, "again.png", ALICE_PNG)

    assert (response["duplicate"], response["id"]) == (True, alice_image["id"])


def test_zip_download_includes_only_the_callers_images(bob, alice_image):
    bob_image = _upload(bob, "bob.png", png_bytes(color=(1, 2, 3)))

    only_alices = bob.post("/api/gallery/download-zip", json={"ids": [alice_image["id"]]})
    mixed = bob.post("/api/gallery/download-zip", json={"ids": [alice_image["id"], bob_image["id"]]})

    assert only_alices.status_code == 404
    with zipfile.ZipFile(io.BytesIO(mixed.content)) as zf:
        assert zf.namelist() == ["bob.png"]


def test_the_owner_can_download_their_images_as_a_zip(alice, alice_image):
    response = alice.post("/api/gallery/download-zip", json={"ids": [alice_image["id"]]})

    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert [zf.read(name) for name in zf.namelist()] == [ALICE_PNG]


@pytest.mark.parametrize("path, params", [
    ("/api/gallery/clear-user-tags", {}),
    ("/api/gallery/clear-ai-tags", {}),
    ("/api/gallery/dedupe-tags", {}),
])
def test_bulk_tag_cleanup_leaves_another_users_tags_alone(alice, bob, alice_image, path, params):
    bob.post(path, params=params)

    image = alice.get(f"/api/gallery/{alice_image['id']}").json()
    assert (image["tags"], image["ai_tags"]) == ("passport, travel", "document, face")


def test_clearing_ai_tags_by_image_id_skips_another_users_image(alice, bob, alice_image):
    response = bob.post("/api/gallery/clear-ai-tags", params={"image_id": alice_image["id"]}).json()

    assert response == {"ok": True, "cleared": 0}
    assert alice.get(f"/api/gallery/{alice_image['id']}").json()["ai_tags"] == "document, face"


def test_the_owner_can_clean_up_their_tags(alice, alice_image):
    # A PATCH drops tags the image already has as AI tags, so set the user tag first.
    _set_ai_tags(alice_image["id"], "")
    alice.patch(f"/api/gallery/{alice_image['id']}", json={"tags": "passport, Face"})
    _set_ai_tags(alice_image["id"], "document, face")

    assert alice.post("/api/gallery/dedupe-tags").json() == {"ok": True, "rows_touched": 1, "tags_removed": 1}
    assert alice.post("/api/gallery/clear-ai-tags").json() == {"ok": True, "cleared": 1}
    assert alice.post("/api/gallery/clear-user-tags").json() == {"ok": True, "cleared": 1}
    image = alice.get(f"/api/gallery/{alice_image['id']}").json()
    assert (image["tags"], image["ai_tags"]) == ("", "")
