"""Sorting a user's chats into folders never runs on another user's model.

The background-task and utility settings point at the admin's endpoint. The
sort sends every chat title of its owner to the model, so for alice it must not
resolve to the admin's endpoint and key.
"""
import json

import pytest

import core.database
import routes.prefs_routes
import src.database
import src.endpoint_resolver
import src.llm_core
import src.settings
from src.session_actions import run_auto_sort

pytestmark = pytest.mark.security


@pytest.fixture
def db(app_db, monkeypatch):
    monkeypatch.setattr(core.database, "SessionLocal", app_db.SessionLocal)
    monkeypatch.setattr(src.database, "SessionLocal", app_db.SessionLocal)
    monkeypatch.setattr(src.endpoint_resolver, "SessionLocal", app_db.SessionLocal)
    session = app_db.SessionLocal()
    session.add(src.database.ModelEndpoint(
        id="admin-ep", name="admin endpoint", base_url="http://admin-endpoint.test/v1",
        is_enabled=True, owner="admin", cached_models=json.dumps(["admin-model"]),
    ))
    for i in range(2):
        session.add(core.database.Session(
            id=f"alice-{i}", name=f"alice chat {i}", owner="alice",
            endpoint_url="http://alice-endpoint.test/v1", model="alice-model",
        ))
    session.commit()
    yield session
    session.close()


async def test_sorting_alices_chats_does_not_use_the_admins_endpoint(db, monkeypatch):
    settings = {**src.settings.DEFAULT_SETTINGS}
    for role in ("task", "utility", "default"):
        settings.update({f"{role}_endpoint_id": "admin-ep", f"{role}_model": "admin-model"})
    monkeypatch.setattr(src.settings, "load_settings", lambda: settings)
    monkeypatch.setattr(routes.prefs_routes, "_load_for_user", lambda owner: {})
    calls = []

    async def fake_llm_call_async(url, model, messages, **kwargs):
        calls.append(url)
        return "{}"

    monkeypatch.setattr(src.llm_core, "llm_call_async", fake_llm_call_async)

    summary = await run_auto_sort("alice")

    assert calls == []
    assert "No model endpoint available" in summary
