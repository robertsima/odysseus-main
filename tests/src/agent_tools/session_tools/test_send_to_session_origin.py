"""A message one chat's agent sends to another chat is stored tagged with its origin.

Without the tag the target chat showed an agent's message as a plain "You"
bubble, so the person reading it could not tell which lines their own agent
wrote. The tool runs for real against the app's session manager; only the
model call is faked.
"""
import json

import pytest

import src.database
from src import llm_core
from src.agent_tools import session_tools


@pytest.mark.asyncio
async def test_the_stored_exchange_names_the_chat_it_came_from(api, monkeypatch):
    client = api.as_user("alice")
    db = src.database.SessionLocal()
    try:
        db.add(src.database.ModelEndpoint(
            id="ep-alice", name="Test model", base_url="http://model.test/v1", is_enabled=True,
            owner="alice", cached_models=json.dumps(["m"]),
        ))
        db.commit()
    finally:
        db.close()
    ids = {}
    for name in ("Planner", "Reviewer"):
        created = client.post("/api/session", data={
            "name": name, "skip_validation": "true", "endpoint_id": "ep-alice", "model": "m",
        })
        assert created.status_code == 200, created.text
        ids[name] = created.json()["id"]

    async def model(endpoint_url, model_id, messages, **kwargs):
        return "Looks fine to me."

    monkeypatch.setattr(session_tools, "get_session_manager", lambda: api.session_manager)
    monkeypatch.setattr(llm_core, "llm_call_async", model)

    result = await session_tools.send_to_session(
        json.dumps({"session_id": ids["Reviewer"], "message": "please review the plan", "mode": "chat"}),
        ids["Planner"], owner="alice",
    )

    assert result.get("exit_code", 0) == 0, result
    history = client.get(f"/api/history/{ids['Reviewer']}").json()["history"]
    inbound, reply = [m for m in history if m["role"] in ("user", "assistant")][-2:]
    assert (inbound["content"], reply["content"]) == ("please review the plan", "Looks fine to me.")
    for message, direction in ((inbound, "inbound"), (reply, "reply")):
        meta = message["metadata"]
        assert meta["source"] == "agent"
        assert meta["direction"] == direction
        assert meta["from_session"] == ids["Planner"]
        assert meta["from_session_name"] == "Planner"
