from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import prefs_routes
from routes.stt_routes import setup_stt_routes
from services.stt.stt_service import STTService
from src.auth_helpers import require_user


class FakeSTTService:
    def __init__(self):
        self.calls = []

    def _load_settings(self, owner):
        return {
            "stt_enabled": True,
            "stt_provider": "local",
            "stt_model": "base",
            "stt_language": "",
        }

    def get_stats(self, owner):
        return {"available": True, "local_installed": True, "owner": owner}

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        return {"text": "hello", "provider": "local", "privacy": "local"}


def _client(owner="alice"):
    service = FakeSTTService()
    app = FastAPI()
    app.include_router(setup_stt_routes(service))
    app.dependency_overrides[require_user] = lambda: owner
    return TestClient(app), service


def test_transcribe_uses_authenticated_owner_and_preserves_audio_metadata():
    client, service = _client("alice")

    response = client.post(
        "/api/stt/transcribe",
        files={"file": ("note.ogg", b"synthetic audio", "audio/ogg")},
    )

    assert response.status_code == 200
    assert response.json()["text"] == "hello"
    assert service.calls == [
        (
            b"synthetic audio",
            {"owner": "alice", "filename": "note.ogg", "mime_type": "audio/ogg"},
        )
    ]


def test_transcribe_rejects_non_audio_and_empty_uploads():
    client, service = _client()

    unsupported = client.post(
        "/api/stt/transcribe", files={"file": ("note.txt", b"hello", "text/plain")}
    )
    empty = client.post(
        "/api/stt/transcribe", files={"file": ("note.webm", b"", "audio/webm")}
    )

    assert unsupported.status_code == 415
    assert empty.status_code == 400
    assert service.calls == []


def test_preferences_are_isolated_by_authenticated_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(tmp_path / "prefs.json"))
    app = FastAPI()
    app.include_router(setup_stt_routes(STTService()))
    app.dependency_overrides[require_user] = lambda: "alice"
    client = TestClient(app)

    saved = client.put(
        "/api/stt/preferences",
        json={
            "enabled": True,
            "provider": "browser",
            "model": "base",
            "language": "en-US",
        },
    )
    assert saved.status_code == 200
    assert client.get("/api/stt/preferences").json()["language"] == "en-US"

    app.dependency_overrides[require_user] = lambda: "bob"
    bob = client.get("/api/stt/preferences").json()
    assert bob["provider"] == "local"
    assert bob["language"] == ""


def test_voice_input_ui_exposes_private_local_mode_without_https_requirement():
    root = Path(__file__).resolve().parent.parent
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    settings_js = (root / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    recorder_js = (root / "static" / "js" / "voiceRecorder.js").read_text(
        encoding="utf-8"
    )

    assert 'id="stt-settings-card"' in html
    assert 'id="set-sttProviderSelect"' in html
    assert "/api/stt/preferences" in settings_js
    assert "Odysseus can remain HTTP" in settings_js
    assert "MediaRecorder.isTypeSupported" in recorder_js
    assert "/api/stt/transcribe" in recorder_js
