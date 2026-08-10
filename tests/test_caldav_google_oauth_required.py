import asyncio
import ipaddress
import sys
import types

import pytest

from src import caldav_sync


GOOGLE_EVENTS_URL = "https://apidata.googleusercontent.com/caldav/v2/me@gmail.com/events"
GOOGLE_USER_URL = "https://apidata.googleusercontent.com/caldav/v2/me@gmail.com/user"
LEGACY_GOOGLE_URL = "https://www.google.com/calendar/dav/me@gmail.com/user"


@pytest.mark.parametrize("url", [GOOGLE_EVENTS_URL, GOOGLE_USER_URL, LEGACY_GOOGLE_URL])
def test_google_caldav_urls_are_detected(url):
    assert caldav_sync.is_google_caldav_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://calendar.example.com/dav",
        "https://apidata.googleusercontent.com/not-caldav/v2/me/events",
        "https://www.google.com/accounts/user",
    ],
)
def test_non_google_caldav_urls_are_not_detected(url):
    assert caldav_sync.is_google_caldav_url(url) is False


@pytest.fixture()
def calendar_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.calendar_routes import setup_calendar_routes

    monkeypatch.setattr(
        caldav_sync,
        "_resolve_caldav_host_ips",
        lambda host: [ipaddress.ip_address("142.250.191.138")],
    )
    monkeypatch.setattr("routes.calendar_routes._require_user", lambda request: "alice")
    prefs_mod = types.ModuleType("routes.prefs_routes")
    prefs_mod._load_for_user = lambda owner: {}
    prefs_mod._save_for_user = lambda owner, prefs: None
    monkeypatch.setitem(sys.modules, "routes.prefs_routes", prefs_mod)

    app = FastAPI()
    app.include_router(setup_calendar_routes())
    return TestClient(app)


def test_google_caldav_test_returns_oauth_message_before_basic_auth(calendar_client):
    response = calendar_client.post(
        "/api/calendar/test",
        json={"url": GOOGLE_EVENTS_URL, "username": "me@gmail.com", "password": "app-password"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "OAuth 2.0" in body["error"]
    assert "username/password" in body["error"]


def test_google_caldav_account_save_is_rejected(calendar_client):
    response = calendar_client.post(
        "/api/calendar/config/accounts",
        json={"url": GOOGLE_EVENTS_URL, "username": "me@gmail.com", "password": "app-password"},
    )

    assert response.status_code == 400
    assert "OAuth 2.0" in response.json()["detail"]


def test_sync_skips_google_basic_auth_account_before_dav_client(monkeypatch):
    prefs_mod = types.ModuleType("routes.prefs_routes")
    prefs_mod._load_for_user = lambda owner: {
        "caldav_accounts": [
            {
                "id": "google",
                "label": "Google Calendar",
                "url": GOOGLE_EVENTS_URL,
                "username": "me@gmail.com",
                "password": "enc:pw",
            }
        ]
    }
    monkeypatch.setitem(sys.modules, "routes.prefs_routes", prefs_mod)

    secret_mod = types.ModuleType("src.secret_storage")
    secret_mod.decrypt = lambda value: "plain-password"
    monkeypatch.setitem(sys.modules, "src.secret_storage", secret_mod)
    monkeypatch.setattr(
        caldav_sync,
        "_resolve_caldav_host_ips",
        lambda host: [ipaddress.ip_address("142.250.191.138")],
    )

    called = False

    def fake_sync_blocking(*args, **kwargs):
        nonlocal called
        called = True
        return {"calendars": 1, "events": 1, "deleted": 0, "errors": []}

    monkeypatch.setattr(caldav_sync, "_sync_blocking", fake_sync_blocking)

    result = asyncio.run(caldav_sync.sync_caldav("alice"))

    assert called is False
    assert result["calendars"] == 0
    assert result["events"] == 0
    assert any("Google Calendar" in err and "OAuth 2.0" in err for err in result["errors"])


def test_writeback_skips_google_basic_auth_account_before_dav_client(monkeypatch):
    import src.caldav_writeback as writeback

    prefs_mod = types.ModuleType("routes.prefs_routes")
    prefs_mod._load_for_user = lambda owner: {
        "caldav_accounts": [
            {
                "id": "google",
                "label": "Google Calendar",
                "url": GOOGLE_EVENTS_URL,
                "username": "me@gmail.com",
                "password": "enc:pw",
            }
        ]
    }
    monkeypatch.setitem(sys.modules, "routes.prefs_routes", prefs_mod)

    secret_mod = types.ModuleType("src.secret_storage")
    secret_mod.decrypt = lambda value: "plain-password"
    monkeypatch.setitem(sys.modules, "src.secret_storage", secret_mod)
    monkeypatch.setattr(
        caldav_sync,
        "_resolve_caldav_host_ips",
        lambda host: [ipaddress.ip_address("142.250.191.138")],
    )

    called = False

    def fake_writeback_blocking(*args, **kwargs):
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr(writeback, "_writeback_blocking", fake_writeback_blocking)

    result = asyncio.run(
        writeback.writeback_event("alice", "caldav", "caldav-123", {"uid": "evt-1"})
    )

    assert called is False
    assert result["ok"] is False
    assert "OAuth 2.0" in result["error"]
