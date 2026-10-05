"""Chat-session actions only an admin may take.

Deleting every chat on the server, pointing a chat at a raw model URL (the
server then dials whatever host the request names) and giving a chat a full
server shell are admin decisions. A signed-in user who is not an admin gets
403; an admin is let through.
"""
import pytest

pytestmark = pytest.mark.security

RAW_URL = "http://127.0.0.1:9/v1/chat/completions"


def _create_session(client, name, **fields):
    response = client.post("/api/session", data={"name": name, "skip_validation": "true", **fields})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_a_user_who_is_not_an_admin_cannot_delete_every_chat(api):
    alice, bob = api.as_user("alice"), api.as_user("bob")
    alice_sid = _create_session(alice, "alice's chat")

    response = bob.delete("/api/sessions/all")

    assert response.status_code == 403
    assert [s["id"] for s in alice.get("/api/sessions").json()] == [alice_sid]


def test_an_admin_can_delete_every_chat(api):
    alice = api.as_user("alice")
    _create_session(alice, "alice's chat")
    _create_session(api.as_user("bob"), "bob's chat")

    response = api.as_admin().delete("/api/sessions/all")

    assert (response.status_code, response.json()["count"]) == (200, 2)
    assert alice.get("/api/sessions").json() == []


def test_a_user_who_is_not_an_admin_cannot_start_a_chat_on_a_raw_model_url(api):
    alice = api.as_user("alice")

    response = alice.post("/api/session", data={"name": "x", "endpoint_url": RAW_URL, "skip_validation": "true"})

    assert response.status_code == 403
    assert alice.get("/api/sessions").json() == []


def test_an_admin_can_start_a_chat_on_a_raw_model_url(api):
    admin = api.as_admin()

    response = admin.post("/api/session", data={"name": "x", "endpoint_url": RAW_URL, "skip_validation": "true"})

    assert response.status_code == 200
    listed = {s["id"]: s for s in admin.get("/api/sessions").json()}
    assert listed[response.json()["id"]]["endpoint_url"] == RAW_URL


def test_a_user_who_is_not_an_admin_cannot_switch_their_chat_to_a_raw_model_url(api):
    alice = api.as_user("alice")
    sid = _create_session(alice, "alice's chat")

    response = alice.patch(f"/api/session/{sid}", data={"model": "m", "endpoint_url": RAW_URL})

    assert response.status_code == 403
    listed = {s["id"]: s for s in alice.get("/api/sessions").json()}
    assert listed[sid]["endpoint_url"] == ""


def test_an_admin_can_switch_their_chat_to_a_raw_model_url(api):
    admin = api.as_admin()
    sid = _create_session(admin, "admin's chat")

    response = admin.patch(f"/api/session/{sid}", data={"model": "m", "endpoint_url": RAW_URL})

    assert (response.status_code, response.json()["endpoint_url"]) == (200, RAW_URL)


def test_a_user_who_is_not_an_admin_cannot_give_their_chat_a_server_shell(api):
    alice = api.as_user("alice")
    sid = _create_session(alice, "alice's chat")

    response = alice.patch(f"/api/session/{sid}/settings", json={"shell_access": "host"})

    assert response.status_code == 403
    assert alice.get(f"/api/session/{sid}/settings").json()["shell_access"] != "host"


def test_an_admin_can_give_their_chat_a_server_shell(api):
    admin = api.as_admin()
    sid = _create_session(admin, "admin's chat")

    response = admin.patch(f"/api/session/{sid}/settings", json={"shell_access": "host"})

    assert response.status_code == 200
    assert admin.get(f"/api/session/{sid}/settings").json()["shell_access"] == "host"
