"""A chat can only use a model endpoint its user may see.

A private endpoint carries its owner's API key, and a chat built on it sends
that key with every request, so Bob must not be able to start or switch a chat
onto Alice's endpoint. The refusal must read like an unknown endpoint id.
"""
import uuid

import pytest

pytestmark = pytest.mark.security

ALICE_BASE = "http://127.0.0.1:8002/v1"


def _create_session(client, name, **fields):
    return client.post("/api/session", data={"name": name, "skip_validation": "true", **fields})


@pytest.fixture
def alice_endpoint(api):
    import core.database as database

    endpoint_id = uuid.uuid4().hex
    db = database.SessionLocal()
    try:
        db.add(database.ModelEndpoint(
            id=endpoint_id, name="alice's GPU box", base_url=ALICE_BASE,
            api_key="alice-secret-key", is_enabled=True, owner="alice",
        ))
        db.commit()
    finally:
        db.close()
    return endpoint_id


def test_another_user_cannot_start_a_chat_on_a_private_endpoint(api, alice_endpoint):
    bob = api.as_user("bob")

    on_alices = _create_session(bob, "x", endpoint_id=alice_endpoint)
    on_missing = _create_session(bob, "x", endpoint_id=uuid.uuid4().hex)

    assert on_alices.status_code == 400
    assert on_alices.json() == on_missing.json()
    assert bob.get("/api/sessions").json() == []


def test_another_user_cannot_switch_their_chat_onto_a_private_endpoint(api, alice_endpoint):
    bob = api.as_user("bob")
    sid = _create_session(bob, "bob's chat").json()["id"]

    # The UI sends the endpoint's URL alongside its id; the id decides.
    response = bob.patch(f"/api/session/{sid}", data={
        "model": "m", "endpoint_url": ALICE_BASE + "/chat/completions", "endpoint_id": alice_endpoint,
    })

    assert response.status_code == 400
    listed = {s["id"]: s for s in bob.get("/api/sessions").json()}
    assert listed[sid]["endpoint_url"] == ""


def test_the_owner_can_start_a_chat_on_their_endpoint(api, alice_endpoint):
    alice = api.as_user("alice")

    response = _create_session(alice, "alice's chat", endpoint_id=alice_endpoint, model="m")

    assert response.status_code == 200, response.text
    listed = {s["id"]: s for s in alice.get("/api/sessions").json()}
    assert listed[response.json()["id"]]["endpoint_url"] == ALICE_BASE + "/chat/completions"
