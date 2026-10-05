"""Google Calendar sign-in without a public or HTTPS address.

2026-09-28: connecting Google Calendar needed a public Tailscale Funnel URL.
The Calendar redirect was always built as ``http://<Host>/...``; Google only
accepts https redirects (http just for localhost), and nothing honoured the
``X-Forwarded-Proto`` of an HTTPS proxy. Now:

- from an address Google accepts, the usual redirect flow is used, with the
  scheme the browser really used;
- from anything else (LAN/Tailscale IP, plain http), Google redirects to
  ``http://localhost``, and the user pastes that address back to
  ``/oauth/google/exchange``.
"""

import sys
import types
import urllib.parse

import pytest
from starlette.requests import Request

from src.oauth_redirect import google_redirect_problem, google_redirect_uri


def _request(host, scheme="http", headers=None):
    raw = [(b"host", host.encode())] + [(k.encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "scheme": scheme, "path": "/", "headers": raw,
                    "server": (host.split(":")[0], 80), "client": ("127.0.0.1", 1), "query_string": b""})


# ── the redirect address ───────────────────────────────────────────────────

def test_redirect_follows_the_https_proxy_in_front(monkeypatch):
    monkeypatch.delenv("X_TEST_REDIRECT", raising=False)
    req = _request("nas.tail1234.ts.net", headers={"x-forwarded-proto": "https"})
    assert google_redirect_uri(req, "/cb", "X_TEST_REDIRECT") == "https://nas.tail1234.ts.net/cb"
    req = _request("internal:7000", headers={"x-forwarded-proto": "https, http",
                                             "x-forwarded-host": "odysseus.example.com"})
    assert google_redirect_uri(req, "/cb", "X_TEST_REDIRECT") == "https://odysseus.example.com/cb"
    assert google_redirect_uri(_request("192.168.1.20:7000"), "/cb", "X_TEST_REDIRECT") == "http://192.168.1.20:7000/cb"


def test_an_explicit_redirect_variable_wins(monkeypatch):
    monkeypatch.setenv("X_TEST_REDIRECT", "https://pinned.example.com/cb")
    assert google_redirect_uri(_request("192.168.1.20:7000"), "/cb", "X_TEST_REDIRECT") == "https://pinned.example.com/cb"


@pytest.mark.parametrize("uri, problem", [
    ("http://localhost:7000/cb", None),
    ("http://127.0.0.1:7000/cb", None),
    ("https://nas.tail1234.ts.net/cb", None),
    ("https://odysseus.example.com/cb", None),
    ("http://nas.tail1234.ts.net/cb", "needs_https"),
    ("http://192.168.1.20:7000/cb", "needs_https"),
    ("https://100.70.14.104:7000/cb", "ip_address"),
    ("https://odysseus.nas/cb", "private_hostname"),
    ("https://zima.local/cb", "private_hostname"),
    ("https://nas/cb", "private_hostname"),
])
def test_what_google_refuses_as_a_redirect(uri, problem):
    assert google_redirect_problem(uri) == problem


# ── the calendar endpoints ─────────────────────────────────────────────────

@pytest.fixture()
def cal(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import calendar_routes

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.delenv("GOOGLE_CALENDAR_OAUTH_REDIRECT_URI", raising=False)
    user = {"name": "alice"}
    monkeypatch.setattr(calendar_routes, "_require_user", lambda request: user["name"])
    prefs_mod = types.ModuleType("routes.prefs_routes")
    prefs_mod._load_for_user = lambda owner: {}
    prefs_mod._save_for_user = lambda owner, prefs: None
    monkeypatch.setitem(sys.modules, "routes.prefs_routes", prefs_mod)
    saved = {}
    monkeypatch.setattr("src.caldav_sync._load_caldav_accounts", lambda owner: list(saved.get(owner, [])))
    monkeypatch.setattr("src.caldav_sync._save_caldav_accounts", lambda owner, accts: saved.__setitem__(owner, accts))
    monkeypatch.setattr("routes.email_helpers.make_oauth_state",
                        lambda account_id, owner: f"state-for-{owner}")
    monkeypatch.setattr("routes.email_helpers.verify_oauth_state",
                        lambda state: {"o": state.removeprefix("state-for-")} if state.startswith("state-for-") else None)

    calls = []

    class _Resp:
        def __init__(self, payload, ok=True):
            self._payload, self.is_success = payload, ok

        def raise_for_status(self):
            if not self.is_success:
                raise RuntimeError("HTTP error")

        def json(self):
            return self._payload

    def fake_post(url, data=None, timeout=None):
        calls.append(("post", url, dict(data or {})))
        return _Resp({"access_token": "at", "refresh_token": "rt", "expires_in": 3600})

    def fake_get(url, headers=None, timeout=None):
        calls.append(("get", url, None))
        return _Resp({"email": "robert@example.com"})

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr("src.secret_storage.encrypt", lambda value: f"enc({value})")

    app = FastAPI()
    app.include_router(calendar_routes.setup_calendar_routes())
    return TestClient(app), saved, calls, user


def test_start_from_a_lan_address_uses_the_localhost_paste_flow(cal):
    client, _saved, _calls, _user = cal
    start = client.get("/api/calendar/oauth/google/start", headers={"host": "100.70.14.104:7000"}).json()
    assert start["mode"] == "paste" and start["redirect_uri"] == "http://localhost"
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(start["auth_url"]).query)
    assert params["redirect_uri"] == ["http://localhost"]
    assert params["access_type"] == ["offline"] and params["prompt"] == ["consent"]


def test_start_behind_an_https_proxy_keeps_the_redirect_flow(cal):
    client, _saved, _calls, _user = cal
    start = client.get("/api/calendar/oauth/google/start",
                       headers={"host": "nas.tail1234.ts.net", "x-forwarded-proto": "https"}).json()
    assert start["mode"] == "redirect"
    assert start["redirect_uri"] == "https://nas.tail1234.ts.net/api/calendar/oauth/google/callback"


def test_pasting_the_localhost_address_connects_the_account(cal):
    client, saved, calls, _user = cal
    pasted = "http://localhost/?state=state-for-alice&code=4%2F0AbCd&scope=email"
    res = client.post("/api/calendar/oauth/google/exchange", json={"callback_url": pasted}).json()
    assert res == {"ok": True, "email": "robert@example.com"}
    token_call = next(c for c in calls if c[0] == "post")
    # The code must be redeemed with exactly the redirect it was issued for.
    assert token_call[2]["redirect_uri"] == "http://localhost" and token_call[2]["code"] == "4/0AbCd"
    [account] = saved["alice"]
    assert account["google_email"] == "robert@example.com" and account["oauth_provider"] == "google"
    assert account["oauth_refresh_token"] == "enc(rt)"

    # Reconnecting the same Google account updates it rather than adding one.
    client.post("/api/calendar/oauth/google/exchange", json={"callback_url": pasted})
    assert len(saved["alice"]) == 1


def test_exchange_refuses_another_users_sign_in_and_addresses_without_a_code(cal):
    client, saved, calls, user = cal
    user["name"] = "mallory"
    res = client.post("/api/calendar/oauth/google/exchange",
                      json={"callback_url": "http://localhost/?state=state-for-alice&code=x"}).json()
    assert res["ok"] is False and res["error"] == "invalid_state"
    res = client.post("/api/calendar/oauth/google/exchange",
                      json={"callback_url": "http://localhost/?state=state-for-mallory"}).json()
    assert res["ok"] is False and res["error"] == "missing_code"
    res = client.post("/api/calendar/oauth/google/exchange",
                      json={"callback_url": "http://localhost/?error=access_denied&state=state-for-mallory"}).json()
    assert res["ok"] is False and res["error"] == "google_error"
    assert not saved and not calls


def test_direct_authorize_from_a_refused_address_explains_instead_of_failing_at_google(cal):
    client, _saved, _calls, _user = cal
    r = client.get("/api/calendar/oauth/google/authorize", headers={"host": "192.168.1.20:7000"},
                   follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/?section=integrations&calendar_oauth_error=needs_https"
