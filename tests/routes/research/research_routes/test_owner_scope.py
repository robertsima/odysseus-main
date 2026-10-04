"""Research runs use the caller's own model endpoints and reports.

A model endpoint carries its owner's API key and base URL. The global
settings here point every role (research, utility, default) at the admin's
endpoint, so a lookup that forgets the caller resolves to it. Alice owns an
endpoint of her own; every research run she starts must use that one.
"""
import json
from urllib.parse import urlparse

import pytest

import src.database
import src.settings

pytestmark = pytest.mark.security

ADMIN_HOST = "admin-endpoint.test"
ALICE_HOST = "alice-endpoint.test"


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
def endpoints(api, monkeypatch):
    _add_endpoint("admin", ADMIN_HOST, "admin-model")
    _add_endpoint("alice", ALICE_HOST, "alice-model")
    settings = dict(src.settings.DEFAULT_SETTINGS)
    for role in ("research", "utility", "default", "chat"):
        settings.update({f"{role}_endpoint_id": "admin-ep", f"{role}_model": "admin-model"})
    monkeypatch.setattr(src.settings, "load_settings", lambda: settings)


@pytest.fixture
def research_handler(api, monkeypatch):
    handler = api.app.state.research_handler
    started = []
    monkeypatch.setattr(handler, "start_research", lambda **kwargs: started.append(kwargs))
    monkeypatch.setattr(handler, "_active_tasks", {})
    handler.started = started
    return handler


def test_research_runs_on_the_callers_own_endpoint(api, endpoints, research_handler):
    response = api.as_user("alice").post("/api/research/start", json={"query": "solar panels"})

    assert response.status_code == 200, response.text
    [run] = research_handler.started
    assert urlparse(run["llm_endpoint"]).hostname == ALICE_HOST
    assert "admin-key" not in json.dumps(run["llm_headers"])


def test_research_cannot_name_another_users_endpoint(api, endpoints, research_handler):
    response = api.as_user("alice").post(
        "/api/research/start", json={"query": "solar panels", "endpoint_id": "admin-ep"})

    assert response.status_code == 404
    assert research_handler.started == []


def _alice_research(research_handler, monkeypatch):
    research_handler._active_tasks["rp-alice1"] = {"owner": "alice", "status": "running"}
    monkeypatch.setattr(research_handler, "get_result", lambda sid: "ALICE'S REPORT")
    monkeypatch.setattr(research_handler, "get_sources", lambda sid: [])


def test_another_user_cannot_spin_off_a_research_report(api, endpoints, research_handler, monkeypatch):
    _alice_research(research_handler, monkeypatch)

    response = api.as_user("bob").post("/api/research/spinoff/rp-alice1")

    assert response.status_code == 404
    assert "ALICE'S REPORT" not in response.text
    assert api.as_user("bob").get("/api/sessions").json() == []


def test_a_spin_off_chat_uses_the_owners_endpoint(api, endpoints, research_handler, monkeypatch):
    _alice_research(research_handler, monkeypatch)

    response = api.as_user("alice").post("/api/research/spinoff/rp-alice1")

    assert response.status_code == 200, response.text
    session = api.session_manager.get_session(response.json()["session_id"])
    assert urlparse(session.endpoint_url).hostname == ALICE_HOST
    assert session.owner == "alice"
