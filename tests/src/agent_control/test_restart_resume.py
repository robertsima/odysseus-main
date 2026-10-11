"""A worker found interrupted by an app restart resumes itself, within bounds.

2026-10-10: the app was hard-killed twice while a worker ran a bash command.
Nothing told the worker or its parent chat, and the person typed "finish it"
again and again. The worker's run, its chat and the parent hand-back are real
here; only the model stream and the parent's own continuation turn are faked.
"""
import json
import os
import time

import pytest

from src import agent_activity as act
from src import agent_control, agent_runs, constants

TASK = "build the feature"
COMMAND = "python -m pytest tests -q"


def _sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A parent chat and a worker chat whose run the last process left live."""
    import core.database as database
    import core.models as core_models
    import src.agent_loop as agent_loop
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
    worker.add_message(core_models.ChatMessage("user", TASK, {"source": "dashboard"}))
    chats = {parent.id: parent, worker.id: worker}
    settings = {"p-1": {"max_parallel_workers": 1}, "w-1": {"parent_session": "p-1"}}

    class _Manager:
        def get_session(self, sid):
            return chats.get(sid)

        def save_sessions(self):
            pass

    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: _Manager())
    monkeypatch.setattr(database, "get_session_settings", lambda sid, **kw: dict(settings.get(sid, {})))

    state = {"calls": [], "followups": [], "parent": parent, "worker": worker, "settings": settings,
             "tmp": tmp_path}

    async def model(url, model_id, messages, **kwargs):
        state["calls"].append([dict(m) for m in messages])
        yield _sse({"delta": "Continued with a narrower lane."})
        yield "data: [DONE]\n\n"

    async def continue_parent(manager, parent_id, parent_chat, worker_chat, owner, status="completed"):
        state["followups"].append((parent_id, worker_chat.id, status))

    monkeypatch.setattr(agent_loop, "stream_agent_loop", model)
    monkeypatch.setattr(agent_control, "_continue_parent", continue_parent)

    def crash(*, restart_resumes=0, age_s=600, parent_session="p-1", source="session", command=COMMAND):
        """Leave a live run in the registry, then 'restart': a new process loads the file."""
        data = {"target_session": "w-1", "mode": "agent",
                **({"parent_session": parent_session} if parent_session else {}),
                **({"restart_resumes": restart_resumes} if restart_resumes else {})}
        rid = act.run_started("w-1", source, f"Worker · {TASK}", owner="alice", data=data)
        if command:
            act.note_progress(rid, current_tool="bash", current_command=command, current_target=command)
        act.flush_runs()
        path = os.path.join(str(tmp_path), "agent_activity", "runs.json")
        runs = json.load(open(path, encoding="utf-8"))
        runs[rid]["started_at"] = time.time() - age_s
        json.dump(runs, open(path, "w", encoding="utf-8"))
        act._reset_for_tests()
        return rid

    state["crash"] = crash
    yield state
    act._reset_for_tests()
    agent_runs._EXTERNAL.clear()


async def _settle():
    import asyncio

    tasks = list(agent_control._WORKERS.values())
    if tasks:
        await asyncio.gather(*tasks)
    await asyncio.sleep(0)


def _to_parent(world):
    return [m for m in world["parent"].history if (m.metadata or {}).get("source") == "worker"]


async def test_one_restart_resumes_the_worker_with_a_note_naming_the_command(world):
    old = world["crash"]()

    result = await agent_control.recover_after_restart()
    await _settle()

    assert [r["run_id"] for r in result["resumed"]] == [old]
    assert len(world["calls"]) == 1
    note = world["calls"][0][-1]["content"]
    assert "app restarted" in note and f"bash: {COMMAND}" in note
    assert "python -m tests.run affected" in note
    # The worker's own history is still there: it continues, it does not start over.
    assert any(TASK in str(m.get("content")) for m in world["calls"][0])
    runs = {r["run_id"]: r for r in act.list_runs(session_id="w-1")}
    assert runs[old]["status"] == "interrupted"
    new = next(r for rid, r in runs.items() if rid != old)
    assert new["status"] == "completed"
    assert new["summary"]["restart_resumes"] == 1 and new["summary"]["resumed_from"] == old
    # The parent hears of the restart once, and then of the finished result.
    told = _to_parent(world)
    assert [m.content.split("]")[0] for m in told] == [
        "[Worker Builder was interrupted by an app restart and resumed", "[Worker Builder finished"]


async def test_second_restart_past_the_limit_does_not_resume_and_tells_the_parent_once(world):
    old = world["crash"](restart_resumes=2)  # agent_worker_auto_resume_limit defaults to 2

    result = await agent_control.recover_after_restart()
    await _settle()

    assert result["resumed"] == [] and not world["calls"]
    told = _to_parent(world)
    assert len(told) == 1
    text = told[0].content
    assert text.startswith("[Worker Builder was interrupted by an app restart]")
    assert COMMAND in text and "not resumed" in text
    assert any(line.startswith("Needs user:") for line in text.splitlines())
    assert world["followups"] == [("p-1", "w-1", "interrupted")]
    assert act.get_run(old)["status"] == "interrupted"

    # The next startup finds nothing new to report.
    again = await agent_control.recover_after_restart()
    assert again["resumed"] == [] and len(_to_parent(world)) == 1


async def test_capacity_refusal_tells_the_parent_instead_of_resuming(world):
    world["crash"]()
    act.run_started("other", "session", "Worker: busy", owner="alice", data={"parent_session": "p-1"})

    await agent_control.recover_after_restart()
    await _settle()

    assert not world["calls"]
    text = _to_parent(world)[0].content
    assert "not resumed" in text and "worker limit" in text


@pytest.mark.parametrize("kwargs", [
    {"source": "claude_code"},            # not a worker
    {"parent_session": None},             # a chat's own turn, no parent to report to
    {"age_s": 7 * 3600},                  # too old to be worth continuing blind
])
async def test_runs_that_are_not_recent_workers_are_not_resumed(world, kwargs):
    world["crash"](**kwargs)

    result = await agent_control.recover_after_restart()
    await _settle()

    assert result["resumed"] == [] and not world["calls"]
    assert not agent_control._WORKERS
