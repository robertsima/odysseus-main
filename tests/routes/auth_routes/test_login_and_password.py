"""Login and password change through the real app and the real AuthManager."""
import asyncio

import pytest

from tests.plugins.http_app import PASSWORD

pytestmark = pytest.mark.security

COOKIE = "odysseus_session"
NEW_PASSWORD = "brand-new-password-1"


def _signed_in(client):
    return client.get("/api/auth/status").json()["authenticated"]


def test_a_correct_login_sets_a_cookie_that_signs_the_client_in(api):
    client = api.anonymous()

    response = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})

    assert response.json() == {"ok": True, "username": "alice"}
    assert COOKIE in response.cookies
    assert _signed_in(client)


def test_a_wrong_password_sets_no_cookie(api):
    client = api.anonymous()

    response = client.post("/api/auth/login", json={"username": "alice", "password": "wrong"})

    assert response.status_code == 401
    assert COOKIE not in response.cookies
    assert not _signed_in(client)


def test_a_session_the_manager_refuses_sets_no_cookie(api, monkeypatch):
    # The account vanished between the password check and the session being issued.
    monkeypatch.setattr(api.auth, "create_session_trusted", lambda username: None)
    client = api.anonymous()

    response = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})

    assert response.status_code == 401
    assert COOKIE not in response.cookies


def test_login_runs_the_bcrypt_checks_off_the_event_loop(api, monkeypatch):
    def on_event_loop():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True

    seen = {}

    def watch(name):
        original = getattr(api.auth, name)

        def wrapper(*args, **kwargs):
            seen[name] = on_event_loop()
            return original(*args, **kwargs)

        monkeypatch.setattr(api.auth, name, wrapper)

    watch("verify_password")
    watch("create_session_trusted")

    response = api.anonymous().post("/api/auth/login", json={"username": "bob", "password": PASSWORD})

    assert response.json()["ok"] is True
    # Inline bcrypt freezes every other request for its 100 to 300 ms.
    assert seen == {"verify_password": False, "create_session_trusted": False}


def test_changing_the_password_signs_out_every_other_session(api):
    here, elsewhere = api.as_user("alice"), api.as_user("alice")
    assert _signed_in(elsewhere)

    response = here.post(
        "/api/auth/change-password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )

    assert response.json() == {"ok": True}
    assert _signed_in(here)
    assert not _signed_in(elsewhere)
    signing_in = api.anonymous().post("/api/auth/login", json={"username": "alice", "password": NEW_PASSWORD})
    assert signing_in.json()["ok"] is True


def test_a_wrong_current_password_changes_nothing_and_keeps_other_sessions(api):
    here, elsewhere = api.as_user("alice"), api.as_user("alice")

    response = here.post(
        "/api/auth/change-password",
        json={"current_password": "not-it", "new_password": NEW_PASSWORD},
    )

    assert response.status_code == 400
    assert _signed_in(elsewhere)
    assert api.anonymous().post(
        "/api/auth/login", json={"username": "alice", "password": PASSWORD}
    ).json()["ok"] is True
