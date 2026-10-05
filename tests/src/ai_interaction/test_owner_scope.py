"""The AI tools resolve models only from endpoints the caller may use.

A model endpoint carries its owner's API key. If a tool resolved another
user's endpoint, the call would run on that user's key and account.
"""
import uuid

import httpx
import pytest
from cryptography.fernet import Fernet

import src.database
import src.settings
from src import ai_interaction, secret_storage

pytestmark = pytest.mark.security


@pytest.fixture
def endpoints(app_db, monkeypatch):
    monkeypatch.setattr(src.database, "SessionLocal", app_db.SessionLocal)
    # Encrypt keys with a throwaway key instead of the data folder's app key.
    monkeypatch.setattr(secret_storage, "_fernet", Fernet(Fernet.generate_key()))

    def add(owner, *, base_url="https://api.anthropic.com/v1", model_type="llm"):
        db = app_db.SessionLocal()
        try:
            db.add(src.database.ModelEndpoint(
                id=str(uuid.uuid4()), name=f"{owner}-endpoint", base_url=base_url,
                api_key=f"{owner}-key", is_enabled=True, model_type=model_type, owner=owner,
            ))
            db.commit()
        finally:
            db.close()

    return add


def test_a_user_cannot_resolve_a_model_on_another_users_endpoint(endpoints):
    endpoints("bob")

    with pytest.raises(ValueError):
        ai_interaction._resolve_model("claude-opus-5", owner="alice")


def test_the_owner_resolves_their_own_endpoint_with_their_key(endpoints):
    endpoints("bob")

    _url, model, headers = ai_interaction._resolve_model("claude-opus-5", owner="bob")

    assert model == "claude-opus-5"
    assert headers["x-api-key"] == "bob-key"


async def test_image_generation_does_not_use_another_users_image_endpoint(endpoints, monkeypatch):
    endpoints("bob", base_url="http://127.0.0.1:9/v1", model_type="image")
    probed = []

    def fake_get(url, **kwargs):
        probed.append(url)
        return httpx.Response(200, json={"data": [{"id": "gpt-image-1"}]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(src.settings, "load_settings", lambda: {})

    result = await ai_interaction.do_generate_image("a lighthouse\ngpt-image-1", owner="alice")

    assert probed == []
    assert "No endpoint found" in result["error"]


@pytest.mark.parametrize("tool,content", [
    ("pipeline", "gpt-test | summarize this"),
    ("ui_control", "switch_model gpt-test"),
])
async def test_dispatch_passes_owner_to_model_tools(monkeypatch, tool, content):
    seen = {}

    async def capture(name, content, session_id=None, owner=None):
        seen[name] = {"content": content, "session_id": session_id, "owner": owner}
        return {"ok": True}

    monkeypatch.setattr(
        ai_interaction,
        "do_pipeline",
        lambda content, session_id=None, owner=None: capture("pipeline", content, session_id, owner),
    )
    monkeypatch.setattr(
        ai_interaction,
        "do_ui_control",
        lambda content, session_id=None, owner=None: capture("ui_control", content, session_id, owner),
    )

    _desc, result = await ai_interaction.dispatch_ai_tool(tool, content, session_id="sid1", owner="alice")

    assert result == {"ok": True}
    assert seen[tool]["owner"] == "alice"
    assert seen[tool]["session_id"] == "sid1"
