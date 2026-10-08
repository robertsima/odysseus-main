"""A worker sent back with send_to_session keeps its work when the model fails.

2026-10-08: a worker resumed by hand ran more rounds, then failed again on an
upstream error; send_to_session re-raised and saved nothing, so its next
resume started without those rounds. The tool and run_headless run for real;
only the agent loop's model stream is faked.
"""
import json

import pytest

import src.database
from src.agent_tools import session_tools


@pytest.mark.asyncio
async def test_a_failed_agent_exchange_saves_the_work_it_did(api, monkeypatch):
    import src.agent_loop as agent_loop

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
    for name in ("Lead", "Builder"):
        created = client.post("/api/session", data={
            "name": name, "skip_validation": "true", "endpoint_id": "ep-alice", "model": "m",
        })
        assert created.status_code == 200, created.text
        ids[name] = created.json()["id"]

    async def model(url, model_id, messages, **kwargs):
        yield 'data: {"type": "agent_step", "round": 1}\n\n'
        yield ('data: ' + json.dumps({"type": "tool_output", "tool": "bash", "command": "make docs",
                                       "output": "built", "exit_code": 0}) + "\n\n")
        yield 'data: {"delta": "Docs built."}\n\n'
        yield ('event: error\ndata: {"error": "Upstream protocol error", "status": 502, '
               '"upstream_drop": "protocol"}\n\n')

    monkeypatch.setattr(session_tools, "get_session_manager", lambda: api.session_manager)
    monkeypatch.setattr(agent_loop, "stream_agent_loop", model)

    result = await session_tools.send_to_session(
        json.dumps({"session_id": ids["Builder"], "message": "continue with the docs", "mode": "agent"}),
        ids["Lead"], owner="alice",
    )

    assert result.get("exit_code") == 1, result
    history = client.get(f"/api/history/{ids['Builder']}").json()["history"]
    reply = [m for m in history if m["role"] == "assistant"][-1]
    assert reply["content"].startswith("Docs built.")
    assert reply["metadata"]["status"] == "failed"
    assert reply["metadata"]["tool_events"][0]["command"] == "make docs"
