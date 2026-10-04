"""Manual compaction summarizes a chat with its owner's model, not the admin's.

The global utility setting points at the admin's endpoint. Compacting alice's
chat must not send her conversation to that endpoint on the admin's key.
"""
import json
from urllib.parse import urlparse

import pytest

import src.context_compactor
import src.database
import src.settings
from core.models import ChatMessage

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


@pytest.fixture
def model_calls(api, monkeypatch):
    _add_endpoint("admin", "admin-model")
    _add_endpoint("alice", "alice-model")
    settings = {**src.settings.DEFAULT_SETTINGS, "utility_endpoint_id": "admin-ep", "utility_model": "admin-model"}
    monkeypatch.setattr(src.settings, "load_settings", lambda: settings)
    calls = []

    async def fake_llm_call_async(url, model, messages, **kwargs):
        calls.append({"host": urlparse(url).hostname, "model": model, "headers": kwargs.get("headers") or {}})
        return "summary of the older messages"

    monkeypatch.setattr(src.context_compactor, "llm_call_async", fake_llm_call_async)
    return calls


def test_compacting_a_chat_uses_the_owners_model(api, model_calls):
    alice = api.as_user("alice")
    created = alice.post("/api/session", data={
        "name": "long chat", "endpoint_id": "alice-ep", "model": "alice-model", "skip_validation": "true",
    })
    assert created.status_code == 200, created.text
    session_id = created.json()["id"]
    session = api.session_manager.get_session(session_id)
    for i in range(12):
        session.add_message(ChatMessage("user" if i % 2 == 0 else "assistant", f"message {i}"))

    response = alice.post(f"/api/session/{session_id}/compact")

    assert response.status_code == 200, response.text
    assert [call["host"] for call in model_calls] == ["alice-endpoint.test"]
    assert "admin-key" not in json.dumps(model_calls)
