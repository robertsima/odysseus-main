"""Every route that takes a file upload refuses one over its limit with 413.

A route that reads the whole upload into memory before checking its size lets
one request take the server down. Each route's limit is lowered to a few bytes
and a file one byte over it is posted; a route that reads unbounded accepts it.
"""
import importlib
import json

import pytest

import src.database

LIMIT = 8
OVER = b"x" * (LIMIT + 1)

# (module holding the limit, limit name, path, form field, content type, extra form data)
UPLOADS = [
    ("routes.stt_routes", "STT_MAX_AUDIO_BYTES", "/api/stt/transcribe", "file", "audio/wav", {}),
    ("routes.calendar_routes", "ICS_MAX_BYTES", "/api/calendar/import", "file", "text/calendar", {}),
    ("routes.email_routes", "EMAIL_COMPOSE_UPLOAD_MAX_BYTES", "/api/email/compose-upload", "file",
     "application/octet-stream", {}),
    ("routes.gallery.gallery_routes", "GALLERY_UPLOAD_MAX_BYTES", "/api/gallery/upload", "file",
     "image/png", {}),
    ("routes.gallery.gallery_routes", "GALLERY_TRANSFORM_UPLOAD_MAX_BYTES", "/api/gallery/ai-upscale", "image",
     "image/png", {"scale": "2"}),
    ("routes.gallery.gallery_routes", "GALLERY_TRANSFORM_UPLOAD_MAX_BYTES", "/api/gallery/style-transfer", "image",
     "image/png", {}),
]


@pytest.mark.parametrize("module, limit, path, field, content_type, data", UPLOADS, ids=[u[2] for u in UPLOADS])
def test_an_upload_over_the_limit_is_refused_with_413(api, monkeypatch, module, limit, path, field, content_type, data):
    monkeypatch.setattr(importlib.import_module(module), limit, LIMIT)

    response = api.as_admin().post(path, files={field: ("upload.bin", OVER, content_type)}, data=data)

    assert response.status_code == 413, response.text


def test_a_memory_import_over_the_limit_is_refused_with_413(api, monkeypatch):
    import routes.memory.memory_routes as memory_routes

    monkeypatch.setattr(memory_routes, "MEMORY_IMPORT_MAX_BYTES", LIMIT)
    admin = api.as_admin()
    db = src.database.SessionLocal()
    try:
        db.add(src.database.ModelEndpoint(
            id="ep", name="model", base_url="http://127.0.0.1:9/v1", is_enabled=True,
            owner="admin", cached_models=json.dumps(["m"]),
        ))
        db.commit()
    finally:
        db.close()
    chat = admin.post("/api/session", data={
        "name": "notes", "endpoint_id": "ep", "model": "m", "skip_validation": "true"})
    assert chat.status_code == 200, chat.text

    response = admin.post("/api/memory/import", data={"session": chat.json()["id"]},
                          files={"file": ("notes.txt", OVER, "text/plain")})

    assert response.status_code == 413, response.text
