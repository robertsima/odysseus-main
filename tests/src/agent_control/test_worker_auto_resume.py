"""A worker whose run fails on a transient upstream error resumes itself.

2026-10-07/08: five worker runs on the ChatGPT backend ended on 500/502s. The
failed run's 74 rounds and 135 tool calls were dropped with the error (only
"Worker failed: ..." was saved), so the resumed worker restarted at 15k tokens
from the task alone; and nothing resumed it at all unless the admin model did,
which it stopped doing after two tries.
"""
import asyncio
import json

import pytest

from src import agent_activity as act
from src import agent_control, agent_runs, constants, headless_agent

_PROTOCOL_502 = ('event: error\ndata: {"error": "Upstream protocol error", "status": 502, '
                 '"fallback_eligible": false, "upstream_drop": "protocol"}\n\n')


def _sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


def _error(status, text):
    return f'event: error\ndata: {json.dumps({"status": status, "error": text})}\n\n'


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A worker chat (a real Session) whose model answers from `script`."""
    import core.models as core_models
    import src.agent_loop as agent_loop
    import src.agent_tools.session_tools as session_tools
    import src.ai_interaction as ai_interaction

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(core_models, "_SESSION_MANAGER_INSTANCE", None)
    monkeypatch.setattr(agent_control, "_AUTO_RESUME_DELAYS", (0.01, 0.02), raising=False)
    act._reset_for_tests()
    agent_runs._EXTERNAL.clear()

    chat = core_models.Session(id="w-1", name="Builder", endpoint_url="http://llm.test/v1", model="m",
                               owner="alice")
    chat.context_length = 200_000

    class _Manager:
        def get_session(self, sid):
            return chat if sid == chat.id else None

        def save_sessions(self):
            pass

    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: _Manager())
    monkeypatch.setattr(session_tools, "_new_child_session", lambda *a, **k: (chat, None))

    state = {"script": [], "calls": []}

    async def model(url, model, messages, **kwargs):
        state["calls"].append([dict(m) for m in messages])
        turn = state["script"][min(len(state["calls"]), len(state["script"])) - 1]
        for frame in turn:
            yield frame if isinstance(frame, str) else _sse(frame)

    monkeypatch.setattr(agent_loop, "stream_agent_loop", model)
    state["chat"] = chat
    yield state
    act._reset_for_tests()
    agent_runs._EXTERNAL.clear()


async def _launch(world):
    rec = await agent_control.launch_worker(owner="alice", task="build the feature", preflight=False)
    return rec["run_id"]


_WORK = [
    {"type": "agent_step", "round": 1},
    {"type": "tool_output", "tool": "bash", "command": "pytest -q tests/feature", "output": "3 passed",
     "exit_code": 0},
    {"delta": "Tests pass; next the docs."},
]


async def test_a_worker_cut_off_by_a_502_resumes_with_its_work_in_context(world):
    world["script"] = [[*_WORK, _PROTOCOL_502], [{"delta": "Docs done."}, "data: [DONE]\n\n"]]

    run_id = await _launch(world)
    await agent_control._WORKERS[run_id]

    assert len(world["calls"]) == 2
    resumed = world["calls"][1]
    # The work trail of the failed run is in the resumed run's context...
    assert any("pytest -q tests/feature" in str(m.get("content")) for m in resumed)
    assert any("Tests pass; next the docs." in str(m.get("content")) for m in resumed)
    # ...and the worker is told why it is running again.
    assert "cut off by an upstream model error" in resumed[-1]["content"]
    assert act.get_run(run_id)["status"] == "completed"
    saved = [m for m in world["chat"].history if m.role == "assistant"]
    assert saved[0].metadata["status"] == "interrupted"
    assert saved[0].metadata["tool_events"][0]["command"] == "pytest -q tests/feature"
    assert saved[-1].content == "Docs done."


async def test_a_worker_that_keeps_failing_resumes_twice_then_reports_the_failure(world):
    world["script"] = [[*_WORK, _error(500, "An error occurred while processing your request")]]

    run_id = await _launch(world)
    await agent_control._WORKERS[run_id]

    assert len(world["calls"]) == 3, "one run and two automatic resumes"
    assert act.get_run(run_id)["status"] == "failed"
    last = world["chat"].history[-1]
    assert last.metadata["status"] == "failed" and "HTTP 500" in last.content


@pytest.mark.parametrize("status,text", [(401, "invalid token"), (429, "usage limit reached"),
                                         (400, "invalid request: bad tool schema")])
async def test_a_refused_request_is_not_resumed(world, status, text):
    world["script"] = [[*_WORK, _error(status, text)]]

    run_id = await _launch(world)
    await agent_control._WORKERS[run_id]

    assert len(world["calls"]) == 1
    assert act.get_run(run_id)["status"] == "failed"
    # Still keeps what the run did, on the failed message itself.
    last = world["chat"].history[-1]
    assert last.metadata["tool_events"][0]["command"] == "pytest -q tests/feature"
    assert "Tests pass; next the docs." in last.content


async def test_stop_during_the_pause_cancels_the_resume(world, monkeypatch):
    monkeypatch.setattr(agent_control, "_AUTO_RESUME_DELAYS", (30.0, 120.0), raising=False)
    world["script"] = [[*_WORK, _PROTOCOL_502], [{"delta": "should not run"}, "data: [DONE]\n\n"]]

    run_id = await _launch(world)
    for _ in range(200):
        if any((m.metadata or {}).get("status") == "interrupted" for m in world["chat"].history) \
                and run_id in headless_agent.running_ids():
            break
        await asyncio.sleep(0.01)
    assert (await agent_control.stop_run(run_id, by="from the Workbench"))["stopped"]
    await asyncio.wait_for(agent_control._WORKERS[run_id], timeout=5)

    assert len(world["calls"]) == 1
    assert act.get_run(run_id)["status"] == "cancelled"
    assert "stopped from the Workbench" in world["chat"].history[-1].content
