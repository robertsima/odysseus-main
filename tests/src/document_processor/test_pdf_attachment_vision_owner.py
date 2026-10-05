"""A scanned PDF attached to a chat is read with the sender's vision endpoint.

The vision model is set globally, and both the admin and alice have an
endpoint that serves it. The admin's is registered first, so a lookup that
forgets the sender would pick it and send alice's pages with the admin's key.
"""
import io
import json

import pytest
from PIL import Image

import src.database
import src.document_processor
import src.settings

pytestmark = pytest.mark.security


def scanned_pdf():
    """One page holding only an image, so reading it asks the vision model."""
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
        db.add(src.database.ModelEndpoint(
            id="alice-text", name="alice text", base_url="http://alice-text.test/v1", is_enabled=True,
            owner="alice", cached_models=json.dumps(["plain-text-model"]),
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


def test_a_scanned_pdf_attachment_is_read_with_the_senders_vision_endpoint(api, vision_keys):
    alice = api.as_user("alice")
    created = alice.post("/api/session", data={
        "name": "papers", "endpoint_id": "alice-text", "model": "plain-text-model", "skip_validation": "true",
    })
    assert created.status_code == 200, created.text
    upload = alice.post("/api/upload", files=[("files", ("scan.pdf", scanned_pdf(), "application/pdf"))])
    file_id = upload.json()["files"][0]["id"]

    alice.post("/api/chat_stream", data={
        "message": "summarize this", "session": created.json()["id"], "attachments": json.dumps([file_id]),
    })

    assert vision_keys
    assert set(vision_keys) == {"alice-key"}
