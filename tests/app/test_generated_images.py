"""GET /api/generated-image/{filename} serves only gallery files, to their owner."""
import os

import pytest

import src.database
import src.generated_images
from core.database import GalleryImage

pytestmark = pytest.mark.security

ALICE_IMAGE = "a1a1a1a1a1a1.png"


@pytest.fixture
def images(api):
    root = src.generated_images.GENERATED_IMAGE_DIR
    root.mkdir(parents=True, exist_ok=True)
    (root / ALICE_IMAGE).write_bytes(b"alice's picture")
    db = src.database.SessionLocal()
    try:
        db.add(GalleryImage(id="img-alice", filename=ALICE_IMAGE, prompt="", model="m", owner="alice", is_active=True))
        db.commit()
    finally:
        db.close()
    yield root
    (root / ALICE_IMAGE).unlink(missing_ok=True)


def test_the_owner_gets_the_image_and_the_browser_may_not_sniff_it(api, images):
    response = api.as_user("alice").get(f"/api/generated-image/{ALICE_IMAGE}")

    assert response.status_code == 200
    assert response.content == b"alice's picture"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_another_user_cannot_fetch_the_image_by_its_name(api, images):
    response = api.as_user("bob").get(f"/api/generated-image/{ALICE_IMAGE}")

    assert response.status_code == 404
    assert b"alice's picture" not in response.content


def test_a_link_out_of_the_image_folder_is_not_followed(api, images, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"outside the image folder")
    link = images / "b2b2b2b2b2b2.png"
    try:
        os.symlink(outside, link)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    try:
        response = api.as_user("alice").get("/api/generated-image/b2b2b2b2b2b2.png")
    finally:
        link.unlink()

    assert response.status_code == 400
    assert b"outside the image folder" not in response.content
