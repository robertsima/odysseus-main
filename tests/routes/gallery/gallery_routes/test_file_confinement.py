"""Gallery file operations stay inside the generated-images folder.

A gallery row's filename is stored data. One that reads ``../outside.png``
must not let delete, rotate or download reach the file next to the folder.
"""
import base64
import io
import zipfile

import pytest

import src.database
from core.database import GalleryImage
from routes.gallery import gallery_routes

pytestmark = pytest.mark.security

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@pytest.fixture
def outside_file(api):
    folder = gallery_routes.GALLERY_IMAGE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    outside = folder.parent / "outside.png"
    outside.write_bytes(PNG)
    db = src.database.SessionLocal()
    try:
        db.add(GalleryImage(id="img-escape", filename="../outside.png", prompt="escape", model="m",
                            owner="alice", is_active=True))
        db.commit()
    finally:
        db.close()
    yield outside
    outside.unlink(missing_ok=True)


def test_deleting_the_row_does_not_delete_a_file_outside_the_folder(api, outside_file):
    api.as_user("alice").delete("/api/gallery/img-escape")

    assert outside_file.read_bytes() == PNG


def test_rotating_does_not_rewrite_a_file_outside_the_folder(api, outside_file):
    response = api.as_user("alice").post("/api/gallery/img-escape/rotate", json={"angle": 90})

    assert response.status_code == 400
    assert outside_file.read_bytes() == PNG


def test_a_zip_download_does_not_include_a_file_outside_the_folder(api, outside_file):
    response = api.as_user("alice").post("/api/gallery/download-zip", json={"ids": ["img-escape"]})

    if response.status_code == 200:
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        assert all(archive.read(name) != PNG for name in archive.namelist())
    else:
        assert response.status_code == 400
