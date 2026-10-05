"""A model comparison runs only on endpoints the caller may use.

/api/compare/start copies the chosen endpoint's API key into the two compare
chats. Naming bob's endpoint, by id or by URL, must not hand his key to alice.
"""
import json

import pytest

import src.database

pytestmark = pytest.mark.security


def _add_endpoint(owner):
    db = src.database.SessionLocal()
    try:
        db.add(src.database.ModelEndpoint(
            id=f"{owner}-ep", name=f"{owner} endpoint", base_url=f"http://{owner}-ep.test/v1",
            api_key=f"{owner}-key", is_enabled=True, owner=owner,
            cached_models=json.dumps([f"{owner}-model"]),
        ))
        db.commit()
    finally:
        db.close()


def _start(client, **endpoints):
    return client.post("/api/compare/start", data={
        "prompt": "Which is faster?", "model_a": "m-a", "model_b": "m-b", **endpoints,
    })


def _keys_held_by(api, owner):
    return json.dumps([s.headers for s in api.session_manager.sessions.values() if s.owner == owner])


@pytest.fixture
def endpoints(api):
    _add_endpoint("alice")
    _add_endpoint("bob")


def test_a_comparison_on_your_own_endpoint_carries_your_key(api, endpoints):
    response = _start(api.as_user("alice"), endpoint_a_id="alice-ep", endpoint_b_id="alice-ep")

    assert response.status_code == 200, response.text
    assert "alice-key" in _keys_held_by(api, "alice")


@pytest.mark.parametrize("fields", [
    {"endpoint_a_id": "bob-ep", "endpoint_b_id": "alice-ep"},
    {"endpoint_a": "http://bob-ep.test/v1", "endpoint_b_id": "alice-ep"},
], ids=["by-id", "by-url"])
def test_a_comparison_cannot_use_another_users_endpoint(api, endpoints, fields):
    response = _start(api.as_user("alice"), **fields)

    assert response.status_code in (403, 404)
    assert "bob-key" not in _keys_held_by(api, "alice")
