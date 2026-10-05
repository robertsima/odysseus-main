"""A user reaches only their own chats, and another user's chat looks missing.

Bob's request for Alice's chat must get the answer a made-up chat id gets
(apart from the id echoed in the message), so the response can't confirm the
chat exists. Afterwards Alice's chat must be exactly as she left it.
"""
import uuid

import pytest

pytestmark = pytest.mark.security

ALICE_NAME = "alice's travel plans"
ALICE_TEXT = "Flight lands at 9pm, door code 4417"


def _create_session(client, name):
    response = client.post("/api/session", data={"name": name, "skip_validation": "true"})
    assert response.status_code == 200, response.text
    return response.json()["id"]


@pytest.fixture
def alice(api):
    return api.as_user("alice")


@pytest.fixture
def bob(api):
    return api.as_user("bob")


@pytest.fixture
def alice_sid(alice):
    sid = _create_session(alice, ALICE_NAME)
    injected = alice.post(f"/api/session/{sid}/inject_messages",
                          json={"messages": [{"role": "user", "content": ALICE_TEXT}]})
    assert injected.status_code == 200, injected.text
    return sid


def _shape(response, sid):
    """Status and body with the requested id blanked, for comparing two answers."""
    return response.status_code, response.text.replace(sid, "<id>")


def _alice_view(alice, sid):
    """What Alice sees of her chat: listing row, transcript and settings."""
    listed = {s["id"]: s for s in alice.get("/api/sessions").json()}
    exported = alice.get(f"/api/session/{sid}/export", params={"fmt": "json"})
    settings = alice.get(f"/api/session/{sid}/settings")
    row = listed.get(sid, {})
    return {
        "listed": sid in listed,
        "name": row.get("name"),
        "folder": row.get("folder"),
        "important": row.get("is_important"),
        "messages": [m["content"] for m in exported.json().get("messages", [])] if exported.status_code == 200 else None,
        "settings": settings.json().get("settings") if settings.status_code == 200 else None,
    }


PER_CHAT = [
    ("PATCH", "/api/session/{sid}", {"data": {"name": "bob's now", "folder": "bob"}}),
    ("POST", "/api/session/{sid}/inject_messages", {"json": {"messages": [{"role": "user", "content": "planted"}]}}),
    ("POST", "/api/session/{sid}/delete", {}),
    ("DELETE", "/api/session/{sid}", {}),
    ("POST", "/api/session/{sid}/archive", {}),
    ("POST", "/api/session/{sid}/unarchive", {}),
    ("GET", "/api/session/{sid}/export", {}),
    ("POST", "/api/session/{sid}/important", {"data": {"important": "true"}}),
    ("GET", "/api/session/{sid}/settings", {}),
    ("PATCH", "/api/session/{sid}/settings", {"json": {"approval_mode": None, "disabled_tools": ["bash"]}}),
    ("POST", "/api/session/{sid}/compact", {}),
    ("GET", "/api/session/{sid}/context_info", {}),
]


@pytest.mark.parametrize("method, path, kwargs", PER_CHAT)
def test_another_users_chat_answers_like_a_missing_one(bob, alice_sid, method, path, kwargs):
    missing = str(uuid.uuid4())

    on_alices = bob.request(method, path.format(sid=alice_sid), **kwargs)
    on_missing = bob.request(method, path.format(sid=missing), **kwargs)

    assert on_alices.status_code == 404
    assert _shape(on_alices, alice_sid) == _shape(on_missing, missing)
    assert ALICE_TEXT not in on_alices.text


@pytest.mark.parametrize("method, path, kwargs", [c for c in PER_CHAT if c[0] != "GET"])
def test_another_user_cannot_change_or_delete_a_chat(alice, bob, alice_sid, method, path, kwargs):
    before = _alice_view(alice, alice_sid)

    bob.request(method, path.format(sid=alice_sid), **kwargs)

    assert _alice_view(alice, alice_sid) == before


def test_bulk_delete_skips_another_users_chat(alice, bob, alice_sid):
    bob_sid = _create_session(bob, "bob's scratch")

    result = bob.post("/api/sessions/bulk-delete", json={"ids": [alice_sid, bob_sid]}).json()

    assert result == {"deleted": 1}
    assert alice.get(f"/api/session/{alice_sid}/settings").status_code == 200


def test_another_user_cannot_unarchive_an_archived_chat(alice, bob, alice_sid):
    assert alice.post(f"/api/session/{alice_sid}/archive").json() == {"status": "archived"}

    assert bob.post(f"/api/session/{alice_sid}/unarchive").status_code == 404

    archived = alice.get("/api/sessions/archived").json()
    assert [s["id"] for s in archived["sessions"]] == [alice_sid]


def test_the_archive_browser_lists_only_the_callers_chats(alice, bob, alice_sid):
    alice.post(f"/api/session/{alice_sid}/archive")

    bob_archived = bob.get("/api/sessions/archived").json()
    found_by_name = bob.get("/api/sessions/archived", params={"search": "travel"}).json()

    assert (bob_archived["sessions"], bob_archived["total"]) == ([], 0)
    assert found_by_name["total"] == 0


@pytest.mark.parametrize("path", ["/api/chats/tidy", "/api/sessions/auto-sort"])
def test_tidy_deletes_only_the_callers_throwaway_chats(alice, bob, path):
    alice_throwaway = _create_session(alice, "Incognito")

    bob.post(path, params={"skip_llm": "true"})
    assert alice.get(f"/api/session/{alice_throwaway}/settings").status_code == 200

    result = alice.post(path, params={"skip_llm": "true"}).json()
    assert result["deleted_throwaway"] == 1
    assert alice.get(f"/api/session/{alice_throwaway}/settings").status_code == 404


def test_the_owner_can_rename_star_and_configure_their_chat(alice, alice_sid):
    renamed = alice.patch(f"/api/session/{alice_sid}", data={"name": "trip", "folder": "travel"}).json()
    assert renamed == {"id": alice_sid, "name": "trip", "folder": "travel"}

    starred = alice.post(f"/api/session/{alice_sid}/important", data={"important": "true"}).json()
    assert starred["is_important"] is True

    patched = alice.patch(f"/api/session/{alice_sid}/settings", json={"disabled_tools": ["bash"]}).json()
    assert patched["settings"]["disabled_tools"] == ["bash"]

    view = _alice_view(alice, alice_sid)
    assert (view["name"], view["folder"], view["important"]) == ("trip", "travel", True)


def test_the_owner_can_export_their_chat(alice, alice_sid):
    exported = alice.get(f"/api/session/{alice_sid}/export", params={"fmt": "txt"})

    assert exported.status_code == 200
    assert ALICE_TEXT in exported.text


def test_the_owner_can_archive_and_restore_their_chat(alice, alice_sid):
    assert alice.post(f"/api/session/{alice_sid}/archive").json() == {"status": "archived"}
    assert alice_sid not in {s["id"] for s in alice.get("/api/sessions").json()}

    assert alice.post(f"/api/session/{alice_sid}/unarchive").json() == {"status": "unarchived"}
    assert alice_sid in {s["id"] for s in alice.get("/api/sessions").json()}


@pytest.mark.parametrize("method, path", [("DELETE", "/api/session/{sid}"), ("POST", "/api/session/{sid}/delete")])
def test_the_owner_can_delete_their_chat(alice, alice_sid, method, path):
    assert alice.request(method, path.format(sid=alice_sid)).json() == {"status": "deleted"}

    assert alice.get(f"/api/session/{alice_sid}/settings").status_code == 404


def test_the_owner_gets_past_the_owner_check_on_compact_and_context_info(alice, alice_sid):
    """One message is too few to compact, so the owner's request stops after the owner check."""
    compact = alice.post(f"/api/session/{alice_sid}/compact")

    assert (compact.status_code, compact.json()["detail"]) == (400, "Not enough messages to compact")
    assert alice.get(f"/api/session/{alice_sid}/context_info").json() == {"context_length": None}
