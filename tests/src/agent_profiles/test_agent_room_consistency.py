"""The Agent Control Room opens the same way every time and can always launch.

User report (2026-09-29): "the agent control room UI changes depending on
where it is launched from or what actions are taken. Sometimes I cannot launch
a worker." The room kept one set of view state for its life (the loadout
editor and the archive had no Launch button and the room reopened in them),
a launch left its form disabled on screen, one invalid saved loadout emptied
every picker, and a launch with no model failed with an error meant for the
tool API. There was also no way to reach an agent chat older than a day.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from src import agent_profiles


# ── loadouts ────────────────────────────────────────────────────────────────

def test_one_invalid_loadout_no_longer_hides_the_others(monkeypatch):
    saved = [
        {"name": "Lead Engineer", "model": "gpt-6"},
        {"name": "bad/name!", "model": "x"},
        {"name": "Scout"},
        {"name": "scout"},
    ]
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: saved if key == "agent_profiles" else default)
    profiles, problems = agent_profiles.load_profiles_with_problems()
    assert [p["name"] for p in profiles] == ["Lead Engineer", "Scout"]
    assert len(problems) == 2 and "bad/name!" in problems[0] and "duplicate" in problems[1]
    assert [p["name"] for p in agent_profiles.load_profiles()] == ["Lead Engineer", "Scout"]
    assert agent_profiles.get_profile("lead engineer")["model"] == "gpt-6"


# ── routes ──────────────────────────────────────────────────────────────────

@pytest.fixture
def routes(monkeypatch, make_test_db):
    from sqlalchemy.pool import QueuePool

    import core.database as database
    from routes import agents_routes as ar

    factory = make_test_db(poolclass=QueuePool).SessionLocal
    monkeypatch.setattr(database, "SessionLocal", factory)
    db = factory()
    from datetime import datetime, timedelta
    now = datetime(2026, 9, 29, 12, 0)
    rows = [
        ("w1", "↳ Lead Engineer: fix checkout", "agent", {"agent_profile": "Lead Engineer", "parent_session": "p1"}, 10),
        ("p1", "Admin", "agent", {}, 1),
        ("c1", "Groceries", "chat", {}, 0),
        ("old", "Scout run", None, {"agent_profile": "Scout"}, 60 * 24 * 9),
    ]
    for sid, name, mode, settings, minutes_ago in rows:
        db.add(database.Session(id=sid, name=name, owner="alice", endpoint_url="x", model="m", mode=mode,
                                settings_json=json.dumps(settings), message_count=3,
                                last_message_at=now - timedelta(minutes=minutes_ago)))
    db.add(database.Session(id="b1", name="Bob agent", owner="bob", endpoint_url="x", model="m", mode="agent",
                            settings_json="{}", last_message_at=now))
    db.commit(); db.close()

    owned = {sid: SimpleNamespace(id=sid, name=name, owner="alice", archived=False) for sid, name, *_ in rows}
    mgr = SimpleNamespace(get_sessions_for_user=lambda user: owned if user == "alice" else {})
    monkeypatch.setattr(ar, "effective_user", lambda request: "alice")
    router = ar.setup_agents_routes(mgr)
    return {(m, r.path): r.endpoint for r in router.routes for m in r.methods}


def test_history_lists_agent_chats_of_any_age_newest_first(routes):
    history = routes[("GET", "/api/agents/history")]
    out = asyncio.run(history(SimpleNamespace(), q="", limit=40, offset=0))
    assert [c["id"] for c in out["chats"]] == ["p1", "w1", "old"]
    worker = out["chats"][1]
    assert worker["profile"] == "Lead Engineer" and worker["parent_name"] == "Admin"

    found = asyncio.run(history(SimpleNamespace(), q="scout", limit=40, offset=0))
    assert [c["id"] for c in found["chats"]] == ["old"]


def test_launch_errors_are_worded_for_the_form(routes, monkeypatch):
    from src import agent_control

    async def refuse(**kwargs):
        raise ValueError("session_id 'new' needs a profile with a model or a calling chat to copy the model from")

    monkeypatch.setattr(agent_control, "launch_worker", refuse)
    launch = routes[("POST", "/api/agents/launch")]

    async def body():
        return {"task": "do it"}

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as err:
        asyncio.run(launch(SimpleNamespace(json=body)))
    assert err.value.status_code == 400
    assert "pick a chat to report to" in err.value.detail
