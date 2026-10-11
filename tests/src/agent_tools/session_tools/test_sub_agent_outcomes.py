"""A send_to_session sub-agent is a worker like any other: marked, judged and tied to its outcome.

The tool, run_headless and the hand-back run for real; only the model stream is faked.
"""
import asyncio
import json

import pytest

import src.database
from src import agent_activity as activity
from src import outcomes
from src.agent_tools import session_tools

BLOCKED = "Outcome: blocked\nOpen: deploy\nNeeds user: paste the staging key into note 'deploy'\n"
PARTIAL = "Outcome: partly done\nOpen:\n- wire the retry path\n"
DONE_NO_FACTS = "Outcome: done\nChanged: tidied.\nChecked: it works.\n"


def _chat(client, name):
    created = client.post("/api/session", data={
        "name": name, "skip_validation": "true", "endpoint_id": "ep-alice", "model": "m"})
    assert created.status_code == 200, created.text
    return created.json()["id"]


@pytest.fixture
def world(api, monkeypatch):
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
    state = {"lead": _chat(client, "Lead"), "builder": _chat(client, "Builder"), "report": "", "hold": None,
             "api": api}

    async def model(url, model_id, messages, **kwargs):
        if state["hold"] is not None:
            state["hold"]["started"].set()
            await state["hold"]["release"].wait()
        yield f"data: {json.dumps({'delta': state['report']})}\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(session_tools, "get_session_manager", lambda: api.session_manager)
    monkeypatch.setattr("src.ai_interaction.get_session_manager", lambda: api.session_manager)
    monkeypatch.setattr(agent_loop, "stream_agent_loop", model)

    async def _no_continuation(*a, **k):
        return None

    monkeypatch.setattr("src.agent_control._continue_parent", _no_continuation)
    return state


async def _send(world, message="carry on"):
    return await session_tools.send_to_session(
        json.dumps({"session_id": world["builder"], "message": message, "mode": "agent"}),
        world["lead"], owner="alice")


def _run(world):
    return [r for r in activity.list_runs(session_id=world["lead"]) if r["source"] == "session"][0]


def _serving(world):
    out = outcomes.create("alice", "Ship the parser", links=[{"kind": "chat", "ref": world["lead"]}])["outcome"]
    outcomes.set_state("alice", out["id"], "active", actor="person")
    return out["id"]


async def test_the_run_carries_an_explicit_sub_agent_marker(world):
    world["report"] = "Outcome: done\nChecked: 12 passed\n"

    await _send(world)

    assert _run(world)["summary"]["kind"] == "sub_agent"


async def test_a_blocked_report_is_a_blocked_run_and_the_tool_says_so(world):
    world["report"] = BLOCKED

    out = await _send(world)

    assert out["status"] == "blocked" and out["needs_user"] == ["paste the staging key into note 'deploy'"]
    assert _run(world)["status"] == "blocked"


async def test_done_without_evidence_is_unverified(world):
    world["report"] = DONE_NO_FACTS

    out = await _send(world)

    assert out.get("status") is None and out["verification"] == "unverified"
    assert _run(world)["status"] == "completed" and _run(world)["summary"]["verification"] == "unverified"


async def test_the_sub_agent_joins_the_outcome_its_starting_chat_serves(world):
    oid = _serving(world)
    world["report"] = PARTIAL

    await _send(world)

    got = outcomes.get("alice", oid)
    refs = {(link["kind"], link["ref"]) for link in got["links"]}
    assert ("chat", world["builder"]) in refs and ("agent_run", _run(world)["run_id"]) in refs
    assert got["state"] == "active" and got["next_action"] == "wire the retry path"


async def test_a_blocked_report_blocks_the_outcome_on_the_person(world):
    oid = _serving(world)
    world["report"] = BLOCKED

    await _send(world)

    got = outcomes.get("alice", oid)
    assert got["state"] == "blocked" and got["blocker"]["code"] == "NEEDS_USER"


async def test_the_detached_child_records_its_report_after_the_caller_is_cancelled(world):
    oid = _serving(world)
    world["report"] = PARTIAL
    world["hold"] = {"started": asyncio.Event(), "release": asyncio.Event()}

    turn = asyncio.create_task(_send(world))
    await asyncio.wait_for(world["hold"]["started"].wait(), timeout=5)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    world["hold"]["release"].set()
    for _ in range(300):
        if _run(world)["status"] != "running":
            break
        await asyncio.sleep(0.02)

    assert _run(world)["status"] == "partial"
    got = outcomes.get("alice", oid)
    assert got["next_action"] == "wire the retry path"
    parent = world["api"].session_manager.get_session(world["lead"])
    told = [m for m in parent.history if (m.metadata or {}).get("source") == "worker"]
    assert told and told[0].content.startswith("[Worker Builder finished part of the task]")


async def test_a_sub_agent_refused_by_preflight_blocks_the_outcome(world):
    """Production bug: the refusal was a tool error only; the outcome kept saying 'active'."""
    oid = _serving(world)

    out = await session_tools.send_to_session(
        json.dumps({"session_id": world["builder"], "message": "fix the bug in the repository", "mode": "agent"}),
        world["lead"], owner="alice")

    assert out.get("exit_code") == 1 or out.get("blocked")
    got = outcomes.get("alice", oid)
    assert got["state"] == "blocked" and got["blocker"]["source"]["kind"] == "preflight"
