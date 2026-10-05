"""Calendar quick-parse reads the user's text with that user's own model.

The global settings name the admin's endpoint for every role; alice's own
preferences name hers. Her event text must not go to the admin's endpoint.
"""
import json
from urllib.parse import urlparse

import pytest

import routes.prefs_routes
import src.database
import src.llm_core
import src.settings

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
        return '{"title": "Lunch with Sara", "start": "2026-10-09T13:00:00"}'

    monkeypatch.setattr(src.llm_core, "llm_call_async", fake_llm_call_async)
    return calls


def test_quick_parse_reads_the_text_on_the_owners_model(api, model_calls):
    api.as_user("alice").post("/api/calendar/quick-parse", json={"text": "lunch with sara friday 1pm"})

    assert model_calls
    assert set(model_calls) == {"alice-ep.test"}
