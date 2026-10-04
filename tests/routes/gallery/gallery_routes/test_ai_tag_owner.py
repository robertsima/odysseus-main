"""AI tagging a gallery image uses the owner's own vision endpoint.

The vision model is set globally, and both the admin and alice have an
endpoint that serves it. The admin's is registered first, so a lookup that
forgets the caller would pick it and send alice's photo with the admin's key.
"""
import base64

import httpx
import pytest

import src.database
import src.settings
from core.database import GalleryImage
from routes.gallery import gallery_routes

pytestmark = pytest.mark.security

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@pytest.fixture
def vision_keys(api, monkeypatch):
    folder = gallery_routes.GALLERY_IMAGE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "c3c3c3c3c3c3.png").write_bytes(PNG)
    db = src.database.SessionLocal()
    try:
        for owner in ("admin", "alice"):
            db.add(src.database.ModelEndpoint(
                id=f"{owner}-vision", name=f"{owner} vision", base_url="https://api.anthropic.com/v1",
                api_key=f"{owner}-key", is_enabled=True, owner=owner,
            ))
        db.add(GalleryImage(id="img-alice", filename="c3c3c3c3c3c3.png", prompt="", model="m",
                            owner="alice", is_active=True))
        db.commit()
    finally:
        db.close()
    settings = {**src.settings.DEFAULT_SETTINGS, "vision_enabled": True, "vision_model": "claude-opus-5"}
    monkeypatch.setattr(src.settings, "load_settings", lambda: settings)
    keys = []

    async def send(self, request, **kwargs):
        keys.append(request.headers.get("x-api-key"))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "red, square"}]}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    yield keys
    (folder / "c3c3c3c3c3c3.png").unlink(missing_ok=True)


def test_ai_tagging_uses_the_owners_vision_endpoint(api, vision_keys):
    api.as_user("alice").post("/api/gallery/img-alice/ai-tag")

    assert vision_keys == ["alice-key"]
