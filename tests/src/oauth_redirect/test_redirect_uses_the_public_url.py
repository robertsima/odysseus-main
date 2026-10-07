"""One public URL gives every Google sign-in its redirect address.

A hosted install already names its public origin once (Settings > Public URL,
or APP_PUBLIC_URL), for reminder links and MCP OAuth. The Gmail and Google
Calendar redirects ignored it, so the same origin had to be written twice more
as GOOGLE_OAUTH_REDIRECT_URI and GOOGLE_CALENDAR_OAUTH_REDIRECT_URI, and an
install that set only the public URL sent Google whatever Host header the
proxy passed through.
"""
from starlette.requests import Request

from src import settings
from src.oauth_redirect import google_redirect_uri


def _request(host):
    return Request({"type": "http", "method": "GET", "scheme": "http", "path": "/",
                    "headers": [(b"host", host.encode())], "server": (host, 80),
                    "client": ("127.0.0.1", 1), "query_string": b""})


def test_the_public_url_supplies_both_google_redirects(monkeypatch):
    monkeypatch.delenv("X_TEST_REDIRECT", raising=False)
    monkeypatch.setenv("APP_PUBLIC_URL", "https://agents.example.com/")
    monkeypatch.setattr(settings, "is_setting_overridden", lambda key: False)
    proxy_view = _request("internal:7000")
    assert google_redirect_uri(proxy_view, "/api/email/oauth/google/callback", "X_TEST_REDIRECT") == (
        "https://agents.example.com/api/email/oauth/google/callback")
    assert google_redirect_uri(proxy_view, "/api/calendar/oauth/google/callback", "X_TEST_REDIRECT") == (
        "https://agents.example.com/api/calendar/oauth/google/callback")


def test_a_flow_specific_redirect_still_wins(monkeypatch):
    monkeypatch.setenv("APP_PUBLIC_URL", "https://agents.example.com")
    monkeypatch.setenv("X_TEST_REDIRECT", "https://pinned.example.com/cb")
    assert google_redirect_uri(_request("internal:7000"), "/cb", "X_TEST_REDIRECT") == "https://pinned.example.com/cb"


def test_without_a_public_url_the_browser_address_is_used(monkeypatch):
    monkeypatch.delenv("X_TEST_REDIRECT", raising=False)
    monkeypatch.delenv("APP_PUBLIC_URL", raising=False)
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(settings, "is_setting_overridden", lambda key: False)
    assert google_redirect_uri(_request("localhost:7000"), "/cb", "X_TEST_REDIRECT") == "http://localhost:7000/cb"
