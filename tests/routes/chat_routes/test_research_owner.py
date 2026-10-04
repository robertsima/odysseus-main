"""Deep research started from a chat runs as the chat's owner, on their endpoint.

The global settings point the research role at the admin's endpoint. A
research run that loses the chat's owner resolves to that endpoint (and its
API key), and is recorded with no owner, so its owner cannot follow it.
"""
import json
from urllib.parse import urlparse

import pytest

import src.database
import src.settings

pytestmark = pytest.mark.security


def _add_endpoint(owner, host, model):
    db = src.database.SessionLocal()
    try:
        db.add(src.database.ModelEndpoint(
            id=f"{owner}-ep", name=f"{owner} endpoint", base_url=f"http://{host}/v1",
            api_key=f"{owner}-key", is_enabled=True, owner=owner, cached_models=json.dumps([model]),
        ))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def research_settings(api, monkeypatch):
    _add_endpoint("admin", "admin-endpoint.test", "admin-model")
    _add_endpoint("alice", "alice-endpoint.test", "alice-model")
    settings = {**src.settings.DEFAULT_SETTINGS,
                "research_endpoint_id": "admin-ep", "research_model": "admin-model"}
    monkeypatch.setattr(src.settings, "load_settings", lambda: settings)


@pytest.fixture
def research_handler(api, monkeypatch):
    handler = api.app.state.research_handler
    started = []

    async def synthesize_query(sess, message, *args, **kwargs):
        return message

    monkeypatch.setattr(handler, "_get_session_json", lambda session_id: {"sources": []})
    monkeypatch.setattr(handler, "synthesize_query", synthesize_query)
    monkeypatch.setattr(handler, "start_research", lambda *args, **kwargs: started.append((args, kwargs)))
    monkeypatch.setattr(handler, "get_status", lambda session_id: None)
    handler.started = started
    return handler


def test_research_from_a_chat_runs_as_its_owner_on_their_endpoint(api, research_settings, research_handler):
    alice = api.as_user("alice")
    created = alice.post("/api/session", data={
        "name": "solar", "endpoint_id": "alice-ep", "model": "alice-model", "skip_validation": "true",
    })
    assert created.status_code == 200, created.text

    alice.post("/api/chat_stream", data={
        "message": "compare home solar panels", "session": created.json()["id"], "use_research": "true",
    })

    [(args, kwargs)] = research_handler.started
    endpoint = args[2]
    assert kwargs["owner"] == "alice"
    assert urlparse(endpoint).hostname == "alice-endpoint.test"
