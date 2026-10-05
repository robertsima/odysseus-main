"""A reminder's wording is written by its owner's model.

The global settings name the admin's endpoint for every role; alice's own
preferences name hers. Her reminder text must not go to the admin's endpoint.
"""
import json
from urllib.parse import urlparse

import pytest

import routes.prefs_routes
import src.database
import src.llm_core
import src.settings
from routes.note.note_routes import dispatch_reminder

pytestmark = pytest.mark.security

ALICE_PREFS = {"utility_endpoint_id": "alice-ep", "utility_model": "alice-model",
               "default_endpoint_id": "alice-ep", "default_model": "alice-model"}


@pytest.fixture
def model_calls(api, monkeypatch):
    db = src.database.SessionLocal()
    try:
        for owner in ("admin", "alice"):
            db.add(src.database.ModelEndpoint(
                id=f"{owner}-ep", name=f"{owner} endpoint", base_url=f"http://{owner}-ep.test/v1",
                api_key=f"{owner}-key", is_enabled=True, owner=owner,
                cached_models=json.dumps([f"{owner}-model"]),
            ))
        db.commit()
    finally:
        db.close()
    settings = dict(src.settings.DEFAULT_SETTINGS)
    for role in ("task", "utility", "default"):
        settings.update({f"{role}_endpoint_id": "admin-ep", f"{role}_model": "admin-model"})
    monkeypatch.setattr(src.settings, "load_settings", lambda: settings)
    monkeypatch.setattr(routes.prefs_routes, "_load_for_user",
                        lambda user=None: dict(ALICE_PREFS) if user == "alice" else {})
    calls = []

    async def fake_llm_call_async(url, model, messages, **kwargs):
        calls.append(urlparse(url).hostname)
        return "Time to call the dentist."

    monkeypatch.setattr(src.llm_core, "llm_call_async", fake_llm_call_async)
    return calls


async def test_a_reminder_is_worded_by_the_owners_model(api, model_calls):
    await dispatch_reminder(
        "Dentist", "Call the dentist about Tuesday", "note-1", owner="alice",
        queue_browser=False, persist_dedupe=False,
        settings_override={"reminder_llm_synthesis": True, "reminder_channel": "browser"},
    )

    assert model_calls
    assert set(model_calls) == {"alice-ep.test"}
