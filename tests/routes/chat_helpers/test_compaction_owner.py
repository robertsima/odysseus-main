"""Automatic compaction during a chat summarizes with the chat owner's model.

The global utility setting points at the admin's endpoint. When alice's chat
outgrows its context, its older half must be summarized on her own model, not
sent to the admin's endpoint on the admin's key.
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


def test_a_long_chat_is_compacted_on_its_owners_model(api, monkeypatch):
    _add_endpoint("admin", "admin-model")
    _add_endpoint("alice", "alice-model")
    settings = {**src.settings.DEFAULT_SETTINGS, "utility_endpoint_id": "admin-ep", "utility_model": "admin-model"}
    monkeypatch.setattr(src.settings, "load_settings", lambda: settings)
    # A tiny context window, so the next turn has to compact.
    monkeypatch.setattr(src.context_compactor, "get_context_length", lambda url, model: 200)
    calls = []

    async def fake_llm_call_async(url, model, messages, **kwargs):
        calls.append({"host": urlparse(url).hostname, "headers": kwargs.get("headers") or {}})
        return "summary of the older messages"

    monkeypatch.setattr(src.context_compactor, "llm_call_async", fake_llm_call_async)
    alice = api.as_user("alice")
    created = alice.post("/api/session", data={
        "name": "long chat", "endpoint_id": "alice-ep", "model": "alice-model", "skip_validation": "true",
    })
    assert created.status_code == 200, created.text
    session_id = created.json()["id"]
    session = api.session_manager.get_session(session_id)
    for i in range(12):
        session.add_message(ChatMessage("user" if i % 2 == 0 else "assistant", f"message {i} " + "words " * 40))

    alice.post("/api/chat_stream", data={"message": "and one more thing", "session": session_id})

    assert calls, "the chat turn did not compact"
    assert {call["host"] for call in calls} == {"alice-endpoint.test"}
    assert "admin-key" not in json.dumps(calls)
