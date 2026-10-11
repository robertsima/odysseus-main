"""Workers reach the outcomes they serve: links, hand-back, and the blockers a refusal leaves.

The launch path, preflight and the hand-back are real; the model stream, the
parent's own continuation and the outcome database's location are the only fakes.
"""
import json

import pytest

from src import agent_activity as act
from src import agent_control, agent_runs, constants

PARTIAL = "Outcome: partly done\nOpen:\n- wire the retry path\n"
BLOCKED = "Outcome: blocked\nNeeds user: paste the staging key into note 'deploy'\n"


@pytest.fixture
def world(tmp_path, monkeypatch, app_db):
    import core.database as database
    import core.models as core_models
    import src.agent_loop as agent_loop
    import src.agent_tools.session_tools as session_tools
    import src.ai_interaction as ai_interaction
    from src import outcomes as store

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(database, "SessionLocal", app_db.SessionLocal)
    monkeypatch.setattr(core_models, "_SESSION_MANAGER_INSTANCE", None)
    act._reset_for_tests()
    agent_runs._EXTERNAL.clear()
    parent = core_models.Session(id="p-1", name="Lead", endpoint_url="http://llm.test/v1", model="m", owner="alice")
    worker = core_models.Session(id="w-1", name="Builder", endpoint_url="http://llm.test/v1", model="m",
                                 owner="alice")
    for chat in (parent, worker):
        chat.context_length = 200_000
    chats = {"p-1": parent, "w-1": worker}
    settings = {"p-1": {"max_parallel_workers": 1}}

    class _Manager:
        def get_session(self, sid):
            return chats.get(sid)

        def save_sessions(self):
            pass

    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: _Manager())
    monkeypatch.setattr(session_tools, "_new_child_session", lambda *a, **k: (worker, None))
    monkeypatch.setattr(database, "get_session_settings", lambda sid, **kw: dict(settings.get(sid, {})))
    state = {"report": PARTIAL, "store": store, "parent": parent, "worker": worker, "settings": settings,
             "tmp": tmp_path}

    async def model(url, model_id, messages, **kwargs):
        yield f"data: {json.dumps({'delta': state['report']})}\n\n"
        yield "data: [DONE]\n\n"

    async def continue_parent(*a, **k):
        return None

    monkeypatch.setattr(agent_loop, "stream_agent_loop", model)
    monkeypatch.setattr(agent_control, "_continue_parent", continue_parent)

    def serving(chat="p-1", state_="active"):
        out = store.create("alice", "Ship the parser", links=[{"kind": "chat", "ref": chat}])["outcome"]
        store.set_state("alice", out["id"], state_, actor="person")
        return out["id"]

    state["serving"] = serving
    yield state
    act._reset_for_tests()
    agent_runs._EXTERNAL.clear()


async def _launch(task="build the feature", **kwargs):
    started = await agent_control.launch_worker(owner="alice", task=task, parent_session="p-1", **kwargs)
    await agent_control._WORKERS[started["run_id"]]
    return started


async def test_a_worker_started_from_a_chat_serving_an_outcome_serves_it_too(world):
    oid = world["serving"]()

    started = await _launch(preflight=False)

    refs = {(link["kind"], link["ref"]) for link in world["store"].get("alice", oid)["links"]}
    assert ("chat", "w-1") in refs and ("agent_run", started["run_id"]) in refs


async def test_a_worker_started_from_an_unlinked_chat_links_nothing(world):
    oid = world["serving"](chat="some-other-chat")

    await _launch(preflight=False)

    assert [link["kind"] for link in world["store"].get("alice", oid)["links"]] == ["chat"]


async def test_the_workers_report_moves_the_outcome(world):
    oid = world["serving"]()
    world["report"] = BLOCKED

    await _launch(preflight=False)

    got = world["store"].get("alice", oid)
    assert got["state"] == "blocked" and got["blocker"]["code"] == "NEEDS_USER"


async def test_a_refused_launch_blocks_the_outcome_and_the_next_launch_clears_it(world):
    """Production bug: a preflight refusal vanished into an error string and the outcome kept saying 'active'."""
    from src import worker_preflight

    oid = world["serving"]()

    with pytest.raises(worker_preflight.WorkerBlocked):
        await agent_control.launch_worker(owner="alice", task="fix the bug in the repository",
                                          parent_session="p-1")

    got = world["store"].get("alice", oid)
    assert got["state"] == "blocked" and got["blocker"]["source"]["kind"] == "preflight"

    await _launch(preflight=False)

    got = world["store"].get("alice", oid)
    assert got["blocker"] is None and got["state"] == "active"


def _crash_worker(world, **summary):
    rid = act.run_started("w-1", "session", "Worker · build the feature", owner="alice",
                          data={"target_session": "w-1", "mode": "agent", "parent_session": "p-1", **summary})
    act.flush_runs()
    act._reset_for_tests()
    return rid


async def test_an_interrupted_worker_that_is_not_resumed_blocks_its_outcome(world):
    oid = world["serving"]()
    _crash_worker(world, restart_resumes=2)  # at the limit

    await agent_control.recover_after_restart()

    blocker = world["store"].get("alice", oid)["blocker"]
    assert blocker["code"] == "RUN_INTERRUPTED" and blocker["recovery"] == "retry"


async def test_a_capacity_refusal_after_a_restart_blocks_the_outcome_as_auto_recovery(world):
    oid = world["serving"]()
    _crash_worker(world)
    act.run_started("other", "session", "Worker: busy", owner="alice", data={"parent_session": "p-1"})

    await agent_control.recover_after_restart()

    blocker = world["store"].get("alice", oid)["blocker"]
    assert blocker["code"] == "WORKER_CAPACITY" and blocker["recovery"] == "auto"


async def test_a_resumed_worker_leaves_the_outcome_unblocked(world):
    oid = world["serving"]()
    _crash_worker(world)

    await agent_control.recover_after_restart()
    for task in list(agent_control._WORKERS.values()):
        await task

    got = world["store"].get("alice", oid)
    assert got["blocker"] is None
    assert any(link["kind"] == "chat" and link["ref"] == "w-1" for link in got["links"])
