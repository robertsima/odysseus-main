"""A chat resolves its model id from the endpoint's stored model list, not a live probe.

Group chats resolve the model for every participant turn. Asking a slow local
/models endpoint each time stalls the turn, so the stored list is tried first,
and it must match the way a session names a model: exactly, or by file name
when the session holds a path.
"""
import json
from types import SimpleNamespace

import pytest

from routes import chat_helpers
from tests.routes.chat_routes.agent_turn import agent_turn  # noqa: F401  (fixture)

ENDPOINT = "http://models.test:8080/v1"


@pytest.fixture
def stored_models(app_db, monkeypatch):
    from core.database import ModelEndpoint

    monkeypatch.setattr(chat_helpers, "SessionLocal", app_db.SessionLocal)
    db = app_db.SessionLocal()
    try:
        db.add(ModelEndpoint(
            id="ep1", name="local", base_url=ENDPOINT, is_enabled=True, owner="alice",
            cached_models=json.dumps(["qwen3-8b.gguf", "llama-3-70b"]),
        ))
        db.commit()
    finally:
        db.close()


def _session(model, url=ENDPOINT):
    return SimpleNamespace(endpoint_url=url, model=model, owner="alice")


@pytest.mark.parametrize("requested, expected", [
    ("llama-3-70b", "llama-3-70b"),
    ("/srv/models/qwen3-8b.gguf", "qwen3-8b.gguf"),
    ("unknown-model", None),
])
def test_a_stored_model_list_resolves_the_sessions_model(stored_models, requested, expected):
    assert chat_helpers._normalize_model_id_from_cache(_session(requested)) == expected


def test_another_endpoints_model_list_is_not_used(stored_models):
    session = _session("llama-3-70b", url="http://elsewhere.test:9000/v1")

    assert chat_helpers._normalize_model_id_from_cache(session) is None


def test_a_turn_on_a_stored_model_never_probes_the_endpoint(agent_turn, monkeypatch):
    probed = []
    monkeypatch.setattr(chat_helpers, "normalize_model_id", lambda *args, **kwargs: probed.append(args) or None)

    agent_turn.send({"message": "hello", "mode": "agent"})

    assert probed == []
