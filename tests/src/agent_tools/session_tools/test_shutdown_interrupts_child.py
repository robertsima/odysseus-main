"""A graceful shutdown leaves a running send_to_session child resumable.

A redeploy cancelled the child with its parent turn and recorded it as
cancelled, which startup never resumes and which hands the parent a
"stopped" result. Shutdown now closes the run as interrupted first.
"""
import asyncio
import json

import pytest

import src.database
from src import agent_activity as activity
from src import agent_control
from src.agent_tools import session_tools


@pytest.mark.asyncio
async def test_shutdown_interrupts_an_in_flight_child_instead_of_cancelling_it(api, monkeypatch):
    import src.agent_loop as agent_loop

    client = api.as_user("alice")
    db = src.database.SessionLocal()
    try:
        db.add(src.database.ModelEndpoint(
            id="ep-alice", name="Test model", base_url="http://model.test/v1", is_enabled=True,
            owner="alice", cached_models=json.dumps(["m"])))
        db.commit()
    finally:
        db.close()
    ids = {}
    for name in ("Lead", "Builder"):
        created = client.post("/api/session", data={
            "name": name, "skip_validation": "true", "endpoint_id": "ep-alice", "model": "m"})
        ids[name] = created.json()["id"]
    started = asyncio.Event()

    async def model(url, model_id, messages, **kwargs):
        yield 'data: {"type": "agent_step", "round": 1}\n\n'
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(session_tools, "get_session_manager", lambda: api.session_manager)
    monkeypatch.setattr("src.ai_interaction.get_session_manager", lambda: api.session_manager)
    monkeypatch.setattr(agent_loop, "stream_agent_loop", model)

    turn = asyncio.create_task(session_tools.send_to_session(
        json.dumps({"session_id": ids["Builder"], "message": "carry on", "mode": "agent"}),
        ids["Lead"], owner="alice"))
    await asyncio.wait_for(started.wait(), timeout=5)

    assert await agent_control.interrupt_workers_for_shutdown() == 1
    turn.cancel()  # stop_all cancels the parent turn next
    with pytest.raises(asyncio.CancelledError):
        await turn
    await asyncio.sleep(0.1)

    run = [r for r in activity.list_runs(session_id=ids["Lead"]) if r["source"] == "session"][0]
    assert run["status"] == "interrupted"
    assert "the app shut down" in run["summary"]["interrupted_reason"]
    assert not session_tools._CHILDREN and not session_tools._DETACHED
    assert not [m for m in api.session_manager.get_session(ids["Lead"]).history
                if (m.metadata or {}).get("source") == "worker"], "the parent was told the child stopped"
