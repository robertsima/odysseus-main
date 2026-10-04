"""GET /api/sessions lists only the caller's own chats."""
import pytest

pytestmark = pytest.mark.security


def _create_session(client, name):
    response = client.post("/api/session", data={"name": name, "skip_validation": "true"})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _listed_ids(client):
    response = client.get("/api/sessions")
    assert response.status_code == 200, response.text
    return {session["id"] for session in response.json()}


def test_each_user_lists_only_their_own_sessions(api):
    alice, bob = api.as_user("alice"), api.as_user("bob")
    alice_session = _create_session(alice, "alice's plans")
    bob_session = _create_session(bob, "bob's plans")

    assert _listed_ids(alice) == {alice_session}
    assert _listed_ids(bob) == {bob_session}
