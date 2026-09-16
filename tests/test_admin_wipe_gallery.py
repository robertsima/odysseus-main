import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from core.database import Base, GalleryImage, GalleryAlbum
from routes.admin_wipe_routes import setup_admin_wipe_routes
from fastapi import Request

def test_wipe_gallery_clears_albums(monkeypatch):
    # 1. Create a clean in-memory database
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    
    # 2. Create test session factory
    TestSessionLocal = sessionmaker(bind=engine)
    
    # 3. Populate test database with an album and an image linked to it
    db = TestSessionLocal()
    album = GalleryAlbum(id="album-1", name="Trip to Rome")
    image = GalleryImage(id="img-1", filename="rome1.jpg", album_id="album-1")
    db.add(album)
    db.add(image)
    db.commit()
    
    assert db.query(GalleryImage).count() == 1
    assert db.query(GalleryAlbum).count() == 1
    db.close()
    
    # 4. Patch SessionLocal in routes/admin_wipe_routes.py to use our in-memory DB
    import routes.admin_wipe_routes
    monkeypatch.setattr(routes.admin_wipe_routes, "SessionLocal", TestSessionLocal)
    
    # Mock require_admin to bypass auth check (using standard pytest monkeypatch)
    monkeypatch.setattr(routes.admin_wipe_routes, "require_admin", lambda r: None)
    
    # Construct a real FastAPI Request object
    request = Request(scope={"type": "http"})
    
    # 5. Initialize the router and retrieve the handler
    router = setup_admin_wipe_routes(session_manager=None)
    wipe_route = next(r for r in router.routes if r.path == "/api/admin/wipe/{kind}")
    wipe_handler = wipe_route.endpoint
    
    # 6. Execute the wipe logic for gallery
    result = wipe_handler(kind="gallery", request=request)
    
    # 7. Assertions
    db = TestSessionLocal()
    assert db.query(GalleryImage).count() == 0
    # This assertion will fail before the fix because GalleryAlbum rows were not deleted
    assert db.query(GalleryAlbum).count() == 0
    
    # Check returned stats
    assert result["status"] == "deleted"
    assert result["kind"] == "gallery"
    assert result["count"] == 2  # 1 image + 1 album
    
    db.close()


def _wipe_gallery(monkeypatch, tmp_path, filenames, *, write_files=True):
    """Run the gallery wipe against an in-memory DB and a real image dir.

    Returns (result, image_dir) so a caller can assert on what survived.
    """
    import routes.admin_wipe_routes
    import src.generated_images as gen

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(bind=engine)

    image_dir = tmp_path / "generated_images"
    image_dir.mkdir(parents=True, exist_ok=True)
    db = TestSessionLocal()
    for i, name in enumerate(filenames):
        if write_files:
            (image_dir / name).write_bytes(b"image bytes")
        db.add(GalleryImage(id=f"img-{i}", filename=name))
    db.commit()
    db.close()

    monkeypatch.setattr(routes.admin_wipe_routes, "SessionLocal", TestSessionLocal)
    monkeypatch.setattr(routes.admin_wipe_routes, "require_admin", lambda r: None)
    monkeypatch.setattr(gen, "GENERATED_IMAGE_DIR", image_dir)

    router = setup_admin_wipe_routes(session_manager=None)
    handler = next(r for r in router.routes
                   if r.path == "/api/admin/wipe/{kind}").endpoint
    return handler(kind="gallery", request=Request(scope={"type": "http"})), image_dir


def test_wipe_gallery_deletes_the_image_files(monkeypatch, tmp_path):
    """The wipe used to remove the rows and two directories nothing writes to,
    leaving every image on disk.

    That is not only retention. app.py authorizes /api/generated-image/ by
    looking up the gallery row and *allows* anything with no row ("generated
    but not yet imported"), so wiping the rows turned every retained image into
    one any signed-in user could fetch by filename.
    """
    names = ["a1b2c3d4e5f6.png", "0f1e2d3c4b5a.png"]
    result, image_dir = _wipe_gallery(monkeypatch, tmp_path, names)

    assert result["status"] == "deleted"
    assert result["files_removed"] == 2
    assert sorted(p.name for p in image_dir.iterdir()) == []


def test_wipe_gallery_survives_a_row_whose_file_is_already_gone(monkeypatch, tmp_path):
    """Best-effort per file: the database half has already committed, so a row
    pointing at a file that no longer exists must not turn a successful wipe
    into a 500. It reports only what it actually removed."""
    result, _ = _wipe_gallery(monkeypatch, tmp_path, ["a1b2c3d4e5f6.png"],
                              write_files=False)
    assert result["status"] == "deleted"
    assert result["files_removed"] == 0


def test_wipe_gallery_cannot_reach_outside_the_image_dir(monkeypatch, tmp_path):
    """A malformed stored filename goes through the path-confined resolver, so
    a traversal attempt removes nothing rather than deleting an arbitrary file."""
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"do not delete")

    result, _ = _wipe_gallery(monkeypatch, tmp_path, ["../secret.png"])
    assert result["files_removed"] == 0
    assert outside.exists()
