"""Importing memories from a scanned PDF uses the importer's vision endpoint.

The vision model is set globally, and both the admin and alice have an
endpoint that serves it. The admin's is registered first, so a lookup that
forgets the caller would pick it and send alice's pages with the admin's key.
"""
import io

import pytest
from PIL import Image

import src.database
import src.document_processor
import src.settings

pytestmark = pytest.mark.security


def scanned_pdf():
    """One page holding only an image, so the importer asks the vision model."""
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (200, 30, 30)).save(buf, "PDF")
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
    # Memory import also needs a model for the extraction step that follows.
    settings = {**src.settings.DEFAULT_SETTINGS, "vision_enabled": True, "vision_model": "claude-opus-5",
                "task_endpoint_id": "alice-vision", "task_model": "claude-opus-5"}
    monkeypatch.setattr(src.settings, "load_settings", lambda: settings)
    keys = []

    def fake_llm_call(url, model, messages, headers=None, **kwargs):
        keys.append((headers or {}).get("x-api-key"))
        return "a red square"

    monkeypatch.setattr(src.document_processor, "llm_call", fake_llm_call)
    return keys


def test_importing_memories_from_a_scanned_pdf_uses_the_importers_vision_endpoint(api, vision_keys):
    api.as_user("alice").post("/api/memory/import",
                              files={"file": ("scan.pdf", scanned_pdf(), "application/pdf")})

    assert vision_keys == ["alice-key"]
