"""A send_to_session child keeps running when the calling turn is cancelled.

2026-10-10: the parent turn was cancelled one second after it sent a worker its
continuation (a newer message in the chat), and the child exchange, which ran
inline in that turn, died with it. The child is now shielded and, once the
parent is gone, hands its result back to the parent chat like a launched worker.
The tool, run_headless and the hand-back run for real; only the model stream
is faked.
"""
import asyncio
import json

import pytest

import src.database
from src import agent_activity as activity
from src.agent_tools import session_tools


def _chat(client, name):
    created = client.post("/api/session", data={
        "name": name, "skip_validation": "true", "endpoint_id": "ep-alice", "model": "m"})
    assert created.status_code == 200, created.text
    return created.json()["id"]


@pytest.mark.asyncio
async def test_child_finishes_and_hands_back_after_the_parent_turn_is_cancelled(api, monkeypatch):
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
    lead, builder = _chat(client, "Lead"), _chat(client, "Builder")
    started, release, calls = asyncio.Event(), asyncio.Event(), []

    async def model(url, model_id, messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:  # the child's exchange, held mid-flight
            yield 'data: {"type": "agent_step", "round": 1}\n\n'
            started.set()
            await release.wait()
            yield 'data: {"delta": "Docs built."}\n\n'
        else:  # the parent's continuation on the hand-back
            yield 'data: {"delta": "Noted, the docs are built."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(session_tools, "get_session_manager", lambda: api.session_manager)
    monkeypatch.setattr("src.ai_interaction.get_session_manager", lambda: api.session_manager)
    monkeypatch.setattr(agent_loop, "stream_agent_loop", model)

    turn = asyncio.create_task(session_tools.send_to_session(
        json.dumps({"session_id": builder, "message": "continue with the docs", "mode": "agent"}),
        lead, owner="alice"))
    await asyncio.wait_for(started.wait(), timeout=5)
    turn.cancel()  # a newer message in the Lead chat, Stop, or shutdown
    with pytest.raises(asyncio.CancelledError):
        await turn
    release.set()
    for _ in range(300):
        runs = [r for r in activity.list_runs(session_id=lead) if r["source"] == "session"]
        hand_back = [m for m in api.session_manager.get_session(lead).history
                     if (m.metadata or {}).get("source") == "worker"]
        if runs and runs[0]["status"] != "running" and hand_back:
            break
        await asyncio.sleep(0.02)

    assert runs[0]["status"] == "completed", runs
    assert hand_back, "the parent chat never heard from the child"
    assert hand_back[0].content.startswith("[Worker Builder finished]")
    assert "Docs built." in hand_back[0].content
    child_history = client.get(f"/api/history/{builder}").json()["history"]
    assert [m for m in child_history if m["role"] == "assistant"][-1]["content"] == "Docs built."
