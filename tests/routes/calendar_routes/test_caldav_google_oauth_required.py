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


# --- Expired / revoked Google refresh token --------------------------------
#
# Production regression: Google answered the refresh with 400 invalid_grant,
# caldav_sync logged a warning and returned None, and POST /api/calendar/sync
# still reported 200 with an empty "errors" list, so the UI showed a clean
# sync over a permanently broken account.

REFRESH_ACCOUNT_ID = "85c4ab11-8918-43bf-bcd1-c28004abd412"


class _FakeTokenResponse:
    """Stands in for the httpx response from Google's token endpoint.

    ``body=None`` models the real-world case where the body is absent or not
    JSON at all (a proxy's HTML error page), which the classifier has to
    survive rather than blow up on.
    """

    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def raise_for_status(self):
        import httpx
        raise httpx.HTTPStatusError("token refresh failed", request=None, response=None)

    def json(self):
        if self._body is None:
            raise ValueError("response is not JSON")
        return self._body


@pytest.fixture()
def google_oauth_account(monkeypatch):
    """A Google-OAuth CalDAV account whose cached access token has expired, so
    every token read goes through the refresh path. Returns the live prefs
    holder so tests can assert on what got persisted."""
    state = {
        "prefs": {
            "caldav_accounts": [{
                "id": REFRESH_ACCOUNT_ID,
                "label": "Google Calendar",
                "url": GOOGLE_EVENTS_URL,
                "oauth_provider": "google",
                "oauth_access_token": "enc:stale",
                "oauth_token_expiry": "1",  # long past, forces a refresh
                "oauth_refresh_token": "enc:refresh",
            }]
        }
    }
    prefs_mod = types.ModuleType("routes.prefs_routes")
    prefs_mod._load_for_user = lambda owner: state["prefs"]
    prefs_mod._save_for_user = lambda owner, prefs: state.__setitem__("prefs", prefs)
    monkeypatch.setitem(sys.modules, "routes.prefs_routes", prefs_mod)

    secret_mod = types.ModuleType("src.secret_storage")
    secret_mod.decrypt = lambda value: (value or "").replace("enc:", "")
    secret_mod.encrypt = lambda value: f"enc:{value}"
    monkeypatch.setitem(sys.modules, "src.secret_storage", secret_mod)

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setattr(
        caldav_sync,
        "_resolve_caldav_host_ips",
        lambda host: [ipaddress.ip_address("142.250.191.138")],
    )
    monkeypatch.setattr(
        caldav_sync,
        "_sync_blocking",
        lambda *args, **kwargs: {"calendars": 1, "events": 5, "deleted": 0, "errors": []},
    )
    return state


def _stub_token_endpoint(monkeypatch, status_code, body):
    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeTokenResponse(status_code, body))


@pytest.mark.parametrize(
    "status_code,body,expected",
    [
        # RFC 6749 section 5.2 codes that mean the grant itself is gone.
        (400, {"error": "invalid_grant"}, caldav_sync.TOKEN_TERMINAL),
        (400, {"error": "INVALID_GRANT"}, caldav_sync.TOKEN_TERMINAL),
        (401, {"error": "invalid_client"}, caldav_sync.TOKEN_TERMINAL),
        (400, {"error": "unauthorized_client"}, caldav_sync.TOKEN_TERMINAL),
        # A 400 whose body we can't read still means "credential verdict" —
        # this endpoint never 400s as a transient blip.
        (400, None, caldav_sync.TOKEN_TERMINAL),
        # Retryable: quota, upstream trouble, unreadable 5xx.
        (429, {"error": "rate_limit_exceeded"}, caldav_sync.TOKEN_TRANSIENT),
        (503, None, caldav_sync.TOKEN_TRANSIENT),
        (500, {"error": "backend_error"}, caldav_sync.TOKEN_TRANSIENT),
        # A 400 with an error code we don't recognise isn't proven terminal.
        (400, {"error": "slow_down"}, caldav_sync.TOKEN_TRANSIENT),
    ],
)
def test_google_token_failures_are_classified(status_code, body, expected):
    status, _code = caldav_sync._classify_google_token_failure(
        _FakeTokenResponse(status_code, body)
    )
    assert status == expected


def test_google_token_failure_without_a_response_is_transient():
    # No response at all == DNS/TLS/connect timeout; nothing is proven about
    # the credentials, so the next sync should just try again.
    assert caldav_sync._classify_google_token_failure(None)[0] == caldav_sync.TOKEN_TRANSIENT


def test_revoked_refresh_token_surfaces_as_a_sync_auth_error(monkeypatch, google_oauth_account):
    _stub_token_endpoint(
        monkeypatch, 400,
        {"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
    )

    result = asyncio.run(caldav_sync.sync_caldav("alice"))

    assert result["events"] == 0
    assert any("reconnect" in err.lower() for err in result["errors"])
    assert [e["account_id"] for e in result["auth_errors"]] == [REFRESH_ACCOUNT_ID]
    assert result["auth_errors"][0]["provider"] == "google"
    # The surfaced error names the account, never the credential.
    blob = repr(result)
    assert "Google Calendar" in blob
    assert "enc:" not in blob


def test_revoked_refresh_token_flags_the_account_for_reconnect(monkeypatch, google_oauth_account):
    _stub_token_endpoint(monkeypatch, 400, {"error": "invalid_grant"})

    asyncio.run(caldav_sync.sync_caldav("alice"))

    acc = google_oauth_account["prefs"]["caldav_accounts"][0]
    assert acc.get("oauth_needs_reconnect") is True


def test_flagged_account_does_not_re_hit_the_token_endpoint(monkeypatch, google_oauth_account):
    """Once Google has said invalid_grant, asking again just earns another 400
    every sync — the stored flag short-circuits until the user reconnects."""
    calls = []

    def counting_post(*args, **kwargs):
        calls.append(1)
        return _FakeTokenResponse(400, {"error": "invalid_grant"})

    import httpx
    monkeypatch.setattr(httpx, "post", counting_post)

    asyncio.run(caldav_sync.sync_caldav("alice"))
    asyncio.run(caldav_sync.sync_caldav("alice"))

    assert len(calls) == 1


def test_transient_refresh_failure_is_not_reported_as_needing_reconnect(
    monkeypatch, google_oauth_account
):
    _stub_token_endpoint(monkeypatch, 503, None)

    result = asyncio.run(caldav_sync.sync_caldav("alice"))

    assert result["auth_errors"] == []
    assert any("temporarily" in err for err in result["errors"])
    assert "oauth_needs_reconnect" not in google_oauth_account["prefs"]["caldav_accounts"][0]


def test_successful_refresh_clears_a_stale_reconnect_flag(monkeypatch, google_oauth_account):
    class _OK:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "fresh-token", "expires_in": 3600}

    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _OK())

    acc = google_oauth_account["prefs"]["caldav_accounts"][0]
    # Call the refresh directly: _google_caldav_token_status deliberately
    # short-circuits a flagged account, so this exercises the cleanup that runs
    # when the flag is cleared some other way (e.g. a manual reconnect).
    acc["oauth_needs_reconnect"] = True
    token, status = caldav_sync._refresh_google_caldav_token_status(
        "alice", REFRESH_ACCOUNT_ID, "refresh"
    )

    assert status == caldav_sync.TOKEN_OK
    assert token == "fresh-token"
    assert "oauth_needs_reconnect" not in google_oauth_account["prefs"]["caldav_accounts"][0]


def test_sync_both_direction_carries_errors_to_the_top_level(monkeypatch, google_oauth_account):
    """The nested {"push":..., "pull":...} shape had no top-level "errors", so
    the route read an empty list and answered 200 over a failed sync."""
    _stub_token_endpoint(monkeypatch, 400, {"error": "invalid_grant"})
    monkeypatch.setattr(caldav_sync, "_pending_writeback_uids", lambda owner: ([], []))

    result = asyncio.run(caldav_sync.sync_caldav_direction("alice", "both"))

    assert result["errors"], "a failed pull must be visible without digging into sub-results"
    assert result["auth_errors"]
    # The breakdown is still there for callers that want it.
    assert "push" in result and "pull" in result


def test_writeback_reports_a_revoked_grant_as_a_terminal_auth_error(
    monkeypatch, google_oauth_account
):
    import src.caldav_writeback as writeback

    _stub_token_endpoint(monkeypatch, 400, {"error": "invalid_grant"})
    monkeypatch.setattr(
        writeback, "_writeback_blocking",
        lambda *a, **k: pytest.fail("should not reach the server"),
    )

    result = asyncio.run(
        writeback.writeback_event("alice", "caldav", "caldav-123", {"uid": "evt-1"})
    )

    assert result["ok"] is False
    assert result["auth_error"]["account_id"] == REFRESH_ACCOUNT_ID


def test_writeback_transient_refresh_failure_has_no_auth_error(monkeypatch, google_oauth_account):
    import src.caldav_writeback as writeback

    _stub_token_endpoint(monkeypatch, 503, None)

    result = asyncio.run(
        writeback.writeback_event("alice", "caldav", "caldav-123", {"uid": "evt-1"})
    )

    assert result["ok"] is False
    assert "auth_error" not in result


def test_accounts_listing_marks_a_revoked_google_account_as_needing_reconnect(
    monkeypatch, google_oauth_account
):
    """The Settings card renders `needs_reconnect`; before this it only checked
    for a *missing* refresh token, so a revoked one still showed "Connected"."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.calendar_routes import setup_calendar_routes

    monkeypatch.setattr("routes.calendar_routes._require_user", lambda request: "alice")
    _stub_token_endpoint(monkeypatch, 400, {"error": "invalid_grant"})

    app = FastAPI()
    app.include_router(setup_calendar_routes())
    client = TestClient(app)

    before = client.get("/api/calendar/config/accounts").json()["accounts"][0]
    assert before["needs_reconnect"] is False

    asyncio.run(caldav_sync.sync_caldav("alice"))

    after = client.get("/api/calendar/config/accounts").json()["accounts"][0]
    assert after["needs_reconnect"] is True


def test_sync_endpoint_surfaces_auth_errors(monkeypatch, google_oauth_account):
    """End to end: the 200 response now carries the terminal failure instead of
    looking like a clean sync."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.calendar_routes import setup_calendar_routes

    monkeypatch.setattr("routes.calendar_routes._require_user", lambda request: "alice")
    _stub_token_endpoint(monkeypatch, 400, {"error": "invalid_grant"})

    todoist_mod = types.ModuleType("src.todoist_calendar_sync")

    async def _no_todoist(owner):
        return {"calendars": 0, "events": 0, "deleted": 0, "errors": [], "skipped": True}

    todoist_mod.sync_todoist_calendar = _no_todoist
    monkeypatch.setitem(sys.modules, "src.todoist_calendar_sync", todoist_mod)

    app = FastAPI()
    app.include_router(setup_calendar_routes())
    response = TestClient(app).post("/api/calendar/sync")

    assert response.status_code == 200
    body = response.json()
    assert body["auth_errors"], "a dead refresh token must not look like a clean sync"
    assert body["auth_errors"][0]["account_id"] == REFRESH_ACCOUNT_ID
    assert any("reconnect" in err.lower() for err in body["errors"])


def test_missing_server_oauth_client_is_not_blamed_on_the_user(monkeypatch, google_oauth_account):
    """No GOOGLE_OAUTH_CLIENT_ID on the server is the operator's problem —
    telling the user to reconnect would just walk them into a broken flow."""
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)

    result = asyncio.run(caldav_sync.sync_caldav("alice"))

    assert result["auth_errors"] == []
    assert any("not configured on this server" in err for err in result["errors"])
    assert "oauth_needs_reconnect" not in google_oauth_account["prefs"]["caldav_accounts"][0]
