"""CalDAV accounts: the URL is vetted, the password is stored encrypted, redirects are refused.

A saved CalDAV account makes the server fetch a user-supplied URL, so it must
reject internal addresses; a password stored in clear text would sit readable in
the accounts file; and the connection test follows no redirect, since a public
URL could otherwise bounce the server into private space.
"""
import ipaddress

import httpx
import pytest

from src import caldav_sync
from src.secret_storage import decrypt

pytestmark = pytest.mark.security

PUBLIC_URL = "https://calendar.example.com/dav/"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_ALLOW_PRIVATE_CALDAV", raising=False)
    monkeypatch.setattr(caldav_sync, "_resolve_caldav_host_ips",
                        lambda host: [ipaddress.ip_address("93.184.216.34")])


def _add(client, url, password="hunter2"):
    return client.post("/api/calendar/config/accounts", json={
        "url": url, "username": "alice", "password": password, "label": "Work",
    })


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:5232/dav", "http://169.254.169.254/latest", "https://alice:pw@calendar.example.com/dav",
])
def test_an_account_pointing_at_an_internal_address_is_refused_and_not_saved(api, url):
    response = _add(api.as_user("alice"), url)

    assert response.status_code == 400
    assert caldav_sync._load_caldav_accounts("alice") == []


def test_the_password_is_stored_encrypted_and_never_returned(api):
    client = api.as_user("alice")

    assert _add(client, PUBLIC_URL).status_code == 200

    stored = caldav_sync._load_caldav_accounts("alice")[0]["password"]
    assert stored != "hunter2"
    assert decrypt(stored) == "hunter2"
    listed = client.get("/api/calendar/config/accounts").json()["accounts"]
    assert listed[0]["has_password"] is True
    assert "hunter2" not in str(listed) and stored not in str(listed)


def test_the_connection_test_does_not_follow_a_redirect(api, monkeypatch):
    requested = []

    def server(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://10.0.0.5/internal"})

    real = httpx.AsyncClient

    class Mocked(real):
        def __init__(self, **kwargs):
            super().__init__(**{**kwargs, "transport": httpx.MockTransport(server)})

    monkeypatch.setattr(httpx, "AsyncClient", Mocked)

    response = api.as_user("alice").post("/api/calendar/test", json={
        "url": PUBLIC_URL, "username": "alice", "password": "hunter2",
    })

    assert response.json()["ok"] is False
    assert "Redirects are not followed" in response.json()["error"]
    assert requested == [PUBLIC_URL]
