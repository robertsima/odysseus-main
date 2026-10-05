"""A chat repairs its model and credentials only from its owner's endpoints.

Before streaming, /api/chat_stream clears a chat whose endpoint is gone,
fills in a missing model from the endpoint's cached list, and restores
missing auth headers from the endpoint row. Alice and bob both have an
endpoint at the same provider URL, bob's registered first, so a lookup that
ignores the owner finds his model list and API key.
"""
import json

import pytest

import src.database

pytestmark = pytest.mark.security

SHARED = "http://shared-provider.test/v1"


def _add_endpoint(owner, base_url, model):
    db = src.database.SessionLocal()
    try:
        db.add(src.database.ModelEndpoint(
            id=f"{owner}-ep", name=f"{owner} endpoint", base_url=base_url, api_key=f"{owner}-key",
            is_enabled=True, owner=owner, cached_models=json.dumps([model]),
        ))
        db.commit()
    finally:
        db.close()


def _alice_chat(api, endpoint_url):
    session = api.session_manager.create_session(
        session_id="alice-chat", name="alice chat", endpoint_url=endpoint_url, model="", owner="alice",
    )
    session.headers = {}
    return session


def _send(api):
    api.as_user("alice").post("/api/chat_stream", data={"message": "hello", "session": "alice-chat"})
    return api.session_manager.get_session("alice-chat")


def test_a_chat_repairs_its_model_and_key_from_its_owners_endpoint(api):
    _add_endpoint("bob", SHARED, "bob-model")
    _add_endpoint("alice", SHARED, "alice-model")
    _alice_chat(api, SHARED)

    session = _send(api)

    assert session.model == "alice-model"
    assert "alice-key" in json.dumps(session.headers)
    assert "bob-key" not in json.dumps(session.headers)


def test_a_chat_on_an_endpoint_only_another_user_has_is_cleared(api):
    _add_endpoint("bob", "http://bob-only.test/v1", "bob-model")
    _alice_chat(api, "http://bob-only.test/v1")

    session = _send(api)

    assert session.endpoint_url == ""
    assert "bob-key" not in json.dumps(session.headers)
