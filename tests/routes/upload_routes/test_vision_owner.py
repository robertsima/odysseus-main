"""Describing an uploaded image uses the uploader's own vision endpoint.

The vision model is set globally, and both the admin and alice have an
endpoint that serves it. The admin's is registered first, so a lookup that
forgets the caller would pick it and send alice's image with the admin's key.
"""
import io

import pytest
from PIL import Image

import src.database
import src.document_processor
import src.settings

pytestmark = pytest.mark.security


def _png():
    # A colour no other test uploads: identical bytes would reuse another
    # upload and its cached description.
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def vision_keys(api, monkeypatch):
    db = src.database.SessionLocal()
    try:
        for owner in ("admin", "alice"):
            db.add(src.database.ModelEndpoint(
                id=f"{owner}-vision", name=f"{owner} vision", base_url="https://api.anthropic.com/v1",
                api_key=f"{owner}-key", is_enabled=True, owner=owner,
            ))
        db.commit()
    finally:
        db.close()
    settings = {**src.settings.DEFAULT_SETTINGS, "vision_enabled": True, "vision_model": "claude-opus-5"}
    monkeypatch.setattr(src.settings, "load_settings", lambda: settings)
    # The per-IP upload limit counts every upload this process made.
    monkeypatch.setattr(api.module.upload_handler, "upload_rate_log", {})
    keys = []

    def fake_llm_call(url, model, messages, headers=None, **kwargs):
        keys.append((headers or {}).get("x-api-key"))
        return "a red square"

    monkeypatch.setattr(src.document_processor, "llm_call", fake_llm_call)
    return keys


def test_describing_an_upload_uses_the_uploaders_vision_endpoint(api, vision_keys):
    alice = api.as_user("alice")
    uploaded = alice.post("/api/upload", files=[("files", ("square.png", _png(), "image/png"))])
    assert uploaded.status_code == 200, uploaded.text
    file_id = uploaded.json()["files"][0]["id"]

    response = alice.get(f"/api/upload/{file_id}/vision")

    assert response.status_code == 200, response.text
    assert vision_keys == ["alice-key"]
