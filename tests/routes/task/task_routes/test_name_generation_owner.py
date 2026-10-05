"""A new task's name comes from the creator's own recent chat model.

Naming borrows the endpoint and auth headers of the newest chat with a model.
Taken across users, alice's task prompt would go to bob's endpoint on his key.
"""
import json
from urllib.parse import urlparse

import pytest

import src.database
import src.llm_core

pytestmark = pytest.mark.security


def _add_endpoint(owner, model):
    db = src.database.SessionLocal()
    try:
        db.add(src.database.ModelEndpoint(
            id=f"{owner}-ep", name=f"{owner} endpoint", base_url=f"http://{owner}-endpoint.test/v1",
            api_key=f"{owner}-key", is_enabled=True, owner=owner, cached_models=json.dumps([model]),
        ))
        db.commit()
    finally:
        db.close()


def _chat(client, owner):
    response = client.post("/api/session", data={
        "name": f"{owner}'s chat", "endpoint_id": f"{owner}-ep", "model": f"{owner}-model",
        "skip_validation": "true",
    })
    assert response.status_code == 200, response.text


def test_naming_a_task_uses_the_creators_chat_model(api, monkeypatch):
    alice, bob = api.as_user("alice"), api.as_user("bob")
    _add_endpoint("alice", "alice-model")
    _add_endpoint("bob", "bob-model")
    _chat(alice, "alice")
    _chat(bob, "bob")  # the newest chat overall is bob's
    calls = []

    async def fake_llm_call_async(url, model, messages, **kwargs):
        calls.append({"host": urlparse(url).hostname, "headers": kwargs.get("headers") or {}})
        return "Water the plants"

    monkeypatch.setattr(src.llm_core, "llm_call_async", fake_llm_call_async)

    response = alice.post("/api/tasks", json={"prompt": "Remind me to water the plants", "schedule": "daily"})

    assert response.status_code == 200, response.text
    assert [call["host"] for call in calls] == ["alice-endpoint.test"]
    assert "bob-key" not in json.dumps(calls)
