from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.lotus_routes import setup_lotus_routes
from src.auth_helpers import require_user
from src.lotus_checkins import LotusCheckinStore, owner_storage_key


def _checkin(emotion: str = "calm") -> dict:
    return {
        "occurred_at": datetime(2026, 8, 10, 9, 15, tzinfo=UTC),
        "timezone": "America/New_York",
        "emotion_label": emotion,
        "emotion_family": "pleasant_low",
        "valence": 0.6,
        "energy": 0.25,
        "intensity": 0.5,
        "note": "A private synthetic note.",
        "tags": ["morning"],
        "context": {"entry_method": "test"},
    }


def test_owner_databases_are_physically_isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("LOTUS_DATA_DIR", str(tmp_path / "lotus"))

    alice = LotusCheckinStore("alice")
    bob = LotusCheckinStore("bob")
    created = alice.create_checkin(_checkin())

    assert created["emotion_label"] == "calm"
    assert created["note"] == "A private synthetic note."
    assert len(alice.list_checkins()) == 1
    assert bob.list_checkins() == []
    assert alice.directory != bob.directory
    assert "alice" not in str(alice.directory)
    assert alice.directory.name == owner_storage_key("alice")


def test_checkin_delete_and_reminder_preferences(monkeypatch, tmp_path):
    monkeypatch.setenv("LOTUS_DATA_DIR", str(tmp_path / "lotus"))
    store = LotusCheckinStore("alice")
    created = store.create_checkin(_checkin("content"))

    prefs = store.save_preferences(
        {
            "timezone": "America/New_York",
            "reminder_enabled": True,
            "reminder_times": ["20:00"],
            "reminder_weekdays": [0, 1, 2, 3, 4, 5, 6],
            "quiet_start": "22:00",
            "quiet_end": "07:00",
            "snooze_minutes": 30,
        }
    )

    assert prefs["reminder_enabled"] is True
    assert prefs["reminder_times"] == ["20:00"]
    assert store.delete_checkin(created["id"]) is True
    assert store.delete_checkin(created["id"]) is False
    assert store.list_checkins() == []


def test_authenticated_routes_only_read_dependency_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("LOTUS_DATA_DIR", str(tmp_path / "lotus"))
    app = FastAPI()
    app.include_router(setup_lotus_routes())
    app.dependency_overrides[require_user] = lambda: "alice"
    client = TestClient(app)

    body = _checkin()
    body["occurred_at"] = body["occurred_at"].isoformat()
    created = client.post("/api/lotus/checkins", json=body)
    assert created.status_code == 201

    alice = client.get("/api/lotus/checkins")
    assert alice.status_code == 200
    assert [entry["emotion_label"] for entry in alice.json()["checkins"]] == ["calm"]

    app.dependency_overrides[require_user] = lambda: "bob"
    bob = client.get("/api/lotus/checkins")
    assert bob.status_code == 200
    assert bob.json() == {"checkins": []}


def test_route_rejects_naive_timestamp_and_bad_reminder_time(monkeypatch, tmp_path):
    monkeypatch.setenv("LOTUS_DATA_DIR", str(tmp_path / "lotus"))
    app = FastAPI()
    app.include_router(setup_lotus_routes())
    app.dependency_overrides[require_user] = lambda: "alice"
    client = TestClient(app)

    body = _checkin()
    body["occurred_at"] = "2026-08-10T09:15:00"
    assert client.post("/api/lotus/checkins", json=body).status_code == 422
    assert (
        client.put(
            "/api/lotus/preferences",
            json={"timezone": "UTC", "reminder_times": ["not-a-time"]},
        ).status_code
        == 422
    )


def test_lotus_ui_is_wired_into_sidebar_and_app():
    root = Path(__file__).resolve().parent.parent
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (root / "static" / "app.js").read_text(encoding="utf-8")
    lotus_js = (root / "static" / "js" / "lotus.js").read_text(encoding="utf-8")

    assert 'id="tool-lotus-btn"' in html
    assert "import lotusModule from './js/lotus.js'" in app_js
    assert "lotusModule.openLotus()" in app_js
    assert "/api/lotus" in lotus_js
    assert "How are you feeling right now?" in lotus_js
