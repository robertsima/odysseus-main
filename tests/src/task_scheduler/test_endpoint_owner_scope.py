"""A scheduled task runs on its owner's endpoints and credentials only.

The global settings point the background roles at two admin endpoints, and
bob has an endpoint of his own. Alice's tasks must not pick up either one's
URL or API key, as a fallback or as auth headers.
"""
import json
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from cryptography.fernet import Fernet

import core.database
import routes.prefs_routes
import src.agent_loop
import src.database
import src.endpoint_resolver
import src.deep_research
import src.settings
from src import secret_storage
from src.task_scheduler import TaskScheduler

pytestmark = pytest.mark.security

ENDPOINTS = (
    ("admin-ep", "admin"), ("admin-utility-ep", "admin"),
    ("alice-ep", "alice"), ("alice-utility-ep", "alice"), ("bob-ep", "bob"),
)
# Alice picked her own utility model; the admin's global settings name theirs.
ALICE_PREFS = {"utility_endpoint_id": "alice-utility-ep", "utility_model": "alice-model"}


@pytest.fixture
def scheduler(app_db, monkeypatch):
    monkeypatch.setattr(core.database, "SessionLocal", app_db.SessionLocal)
    monkeypatch.setattr(src.database, "SessionLocal", app_db.SessionLocal)
    monkeypatch.setattr(src.endpoint_resolver, "SessionLocal", app_db.SessionLocal)
    # Encrypt keys with a throwaway key instead of the data folder's app key.
    monkeypatch.setattr(secret_storage, "_fernet", Fernet(Fernet.generate_key()))
    db = app_db.SessionLocal()
    for endpoint_id, owner in ENDPOINTS:
        db.add(src.database.ModelEndpoint(
            id=endpoint_id, name=endpoint_id, base_url=f"http://{endpoint_id}.test/v1",
            api_key=f"{owner}-key", is_enabled=True, owner=owner,
            cached_models=json.dumps([f"{owner}-model"]),
        ))
    db.commit()
    db.close()
    settings = dict(src.settings.DEFAULT_SETTINGS)
    for role, endpoint_id in (("task", "admin-ep"), ("research", "admin-ep"),
                              ("utility", "admin-utility-ep"), ("default", "admin-utility-ep")):
        settings.update({f"{role}_endpoint_id": endpoint_id, f"{role}_model": "admin-model"})
    monkeypatch.setattr(src.settings, "load_settings", lambda: settings)
    monkeypatch.setattr(routes.prefs_routes, "_load_for_user",
                        lambda owner: dict(ALICE_PREFS) if owner == "alice" else {})
    return TaskScheduler.__new__(TaskScheduler)


def _task(**fields):
    task = dict(
        id="t1", owner="alice", name="nightly digest", prompt="Summarize my day",
        max_steps=1, endpoint_url=None, model=None, crew_member_id=None, character_id=None,
        session_id=None, task_type="llm",
    )
    return SimpleNamespace(**{**task, **fields})


def test_auth_headers_never_come_from_another_users_endpoint(scheduler):
    headers = scheduler._resolve_endpoint_headers("http://bob-ep.test/v1/chat/completions", "alice")

    assert "bob-key" not in json.dumps(headers)


async def test_an_agent_task_falls_back_only_to_its_owners_endpoints(scheduler, monkeypatch):
    calls = []

    async def fake_stream_agent_loop(**kwargs):
        calls.append(kwargs)
        yield 'data: {"delta": "done"}\n\n'

    monkeypatch.setattr(src.agent_loop, "stream_agent_loop", fake_stream_agent_loop)

    await scheduler._run_agent_loop("http://alice-ep.test/v1/chat/completions", "alice-model", _task(), "s1")

    [call] = calls
    assert "alice-key" in json.dumps(call["headers"])
    fallback_hosts = [urlparse(url).hostname for url, _model, _headers in call["fallbacks"]]
    assert fallback_hosts == ["alice-utility-ep.test"]
    assert "admin-key" not in json.dumps(call["fallbacks"])


async def test_a_research_task_does_not_borrow_the_admins_research_endpoint(scheduler, monkeypatch):
    def researcher(**kwargs):
        raise AssertionError(f"research started on {kwargs['llm_endpoint']}")

    monkeypatch.setattr(src.deep_research, "DeepResearcher", researcher)

    # Alice has set no research endpoint of her own, and the research role
    # does not fall back to her utility model, so the run has none.
    with pytest.raises(RuntimeError, match="No model/endpoint"):
        await scheduler._execute_research_task(_task(task_type="research"), None)
