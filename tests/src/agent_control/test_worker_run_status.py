"""A worker's run status says what its hand-back says, not just that the turn ended.

A worker that reported `Outcome: blocked` or `partly done` was recorded as
`completed` and handed to its parent as "[Worker X finished]". The launch path,
the real hand-back and the parent chat are real here; only the model stream and
the parent's own continuation turn are faked.
"""
import json

import pytest

from src import agent_activity as act
from src import agent_control, agent_runs, constants

BLOCKED = "Outcome: blocked\nOpen: deploy the build\nNeeds user: paste the staging deploy key into note 'deploy'\n"
PARTIAL = "Outcome: partly done\nChanged: src/a.py\nOpen:\n- wire the retry path\n- update the docs\n"
DONE_NO_FACTS = "Outcome: done\nChanged: updated the parser.\nChecked: looked it over, it works.\n"
DONE_WITH_TESTS = "Outcome: done\nChanged: src/parser.py\nChecked: pytest tests/src/parser -q: 129 passed\n"


@pytest.fixture
def world(tmp_path, monkeypatch):
    import core.database as database
    import core.models as core_models
    import src.agent_loop as agent_loop
    import src.agent_tools.session_tools as session_tools
    import src.ai_interaction as ai_interaction

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(core_models, "_SESSION_MANAGER_INSTANCE", None)
    act._reset_for_tests()
    agent_runs._EXTERNAL.clear()
    parent = core_models.Session(id="p-1", name="Lead", endpoint_url="http://llm.test/v1", model="m", owner="alice")
    worker = core_models.Session(id="w-1", name="Builder", endpoint_url="http://llm.test/v1", model="m",
                                 owner="alice")
    for chat in (parent, worker):
        chat.context_length = 200_000
    chats = {"p-1": parent, "w-1": worker}

    class _Manager:
        def get_session(self, sid):
            return chats.get(sid)

        def save_sessions(self):
            pass

    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: _Manager())
    monkeypatch.setattr(session_tools, "_new_child_session", lambda *a, **k: (worker, None))
    monkeypatch.setattr(database, "get_session_settings", lambda sid, **kw: {"max_parallel_workers": 2})
    state = {"report": "", "followups": [], "parent": parent, "worker": worker}

    async def model(url, model_id, messages, **kwargs):
        yield f"data: {json.dumps({'delta': state['report']})}\n\n"
        yield "data: [DONE]\n\n"

    async def continue_parent(manager, parent_id, parent_chat, worker_chat, owner, status="completed"):
        state["followups"].append(status)

    monkeypatch.setattr(agent_loop, "stream_agent_loop", model)
    monkeypatch.setattr(agent_control, "_continue_parent", continue_parent)
    yield state
    act._reset_for_tests()
    agent_runs._EXTERNAL.clear()


async def _run(world, report, **kwargs):
    world["report"] = report
    started = await agent_control.launch_worker(owner="alice", task="build the feature", parent_session="p-1",
                                                preflight=False, **kwargs)
    await agent_control._WORKERS[started["run_id"]]
    return act.get_run(started["run_id"])


def _handback(world):
    return [m for m in world["parent"].history if (m.metadata or {}).get("source") == "worker"][-1].content


async def test_a_blocked_report_is_a_blocked_run_and_the_parent_is_told_so(world):
    run = await _run(world, BLOCKED)

    assert run["status"] == "blocked"
    assert run["summary"]["needs_user"] == ["paste the staging deploy key into note 'deploy'"]
    assert run["summary"]["open_first"] == "deploy the build"
    text = _handback(world)
    assert text.startswith("[Worker Builder is blocked]") and "Needs user:" in text
    assert world["followups"] == ["blocked"]


async def test_a_partial_report_is_a_partial_run(world):
    run = await _run(world, PARTIAL)

    assert run["status"] == "partial" and run["summary"]["open_first"] == "wire the retry path"
    assert _handback(world).startswith("[Worker Builder finished part of the task]")


async def test_done_without_evidence_completes_but_is_marked_unverified(world):
    run = await _run(world, DONE_NO_FACTS)

    assert run["status"] == "completed" and run["summary"]["verification"] == "unverified"
    text = _handback(world)
    assert text.startswith("[Worker Builder finished]") and "Unverified" in text


async def test_done_with_a_passing_test_count_is_verified(world):
    run = await _run(world, DONE_WITH_TESTS)

    assert run["status"] == "completed" and run["summary"]["verification"] == "verified"
    assert "Unverified" not in _handback(world)


async def test_a_report_in_no_known_format_stays_completed_without_a_verdict(world):
    run = await _run(world, "I looked around and fixed the typo.")

    assert run["status"] == "completed" and "verification" not in run["summary"]


async def test_a_workflow_child_keeps_the_status_its_workflow_reads(world):
    """agent_workflows collects results from `completed` children; a new status would drop them."""
    run = await _run(world, BLOCKED, handoff=False, run_metadata={"workflow_id": "wf-1"})

    assert run["status"] == "completed"
