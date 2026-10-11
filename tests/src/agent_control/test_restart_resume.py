"""A worker found interrupted by an app restart resumes itself, within bounds.

2026-10-10: the app was hard-killed twice while a worker ran a bash command.
Nothing told the worker or its parent chat, and the person typed "finish it"
again and again. The worker's run, its chat and the parent hand-back are real
here; only the model stream and the parent's own continuation turn are faked.
"""
import asyncio
import json
import os
import time

import pytest

from src import agent_activity as act
from src import agent_control, agent_runs, constants

TASK = "build the feature"
SUBAGENT_MESSAGE = "Carry on with the plan and report back"
COMMAND = "python -m pytest tests -q"


def _sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A parent chat and a worker chat whose run the last process left live."""
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
    worker.add_message(core_models.ChatMessage("user", TASK, {"source": "dashboard"}))
    chats = {parent.id: parent, worker.id: worker}
    settings = {"p-1": {"max_parallel_workers": 1}, "w-1": {"parent_session": "p-1"}}

    class _Manager:
        def get_session(self, sid):
            return chats.get(sid)

        def save_sessions(self):
            pass

    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: _Manager())
    monkeypatch.setattr(session_tools, "_new_child_session", lambda *a, **k: (worker, None))
    monkeypatch.setattr(database, "get_session_settings", lambda sid, **kw: dict(settings.get(sid, {})))

    state = {"calls": [], "followups": [], "parent": parent, "worker": worker, "settings": settings,
             "tmp": tmp_path, "hold": None}

    async def model(url, model_id, messages, **kwargs):
        state["calls"].append([dict(m) for m in messages])
        if state["hold"] is not None:  # a worker held mid-run
            await state["hold"].wait()
        yield _sse({"delta": "Continued with a narrower lane."})
        yield "data: [DONE]\n\n"

    async def continue_parent(manager, parent_id, parent_chat, worker_chat, owner, status="completed"):
        state["followups"].append((parent_id, worker_chat.id, status))

    monkeypatch.setattr(agent_loop, "stream_agent_loop", model)
    monkeypatch.setattr(agent_control, "_continue_parent", continue_parent)

    def crash(*, restart_resumes=0, age_s=600, parent_session="p-1", source="session", command=COMMAND,
              subagent=False, marker=False, title="Sub-agent · ↳ Lead Engineer: Implement the feature",
              message=SUBAGENT_MESSAGE, workspace=None):
        """Leave a live run in the registry, then 'restart': a new process loads the file."""
        if subagent:
            # What send_to_session records: filed under the PARENT chat, no
            # parent_session key, the worker chat in target_session.
            rid = act.run_started("p-1", "session", title,
                                  owner="alice", detail=message,
                                  data={"target_session": "w-1", "target_session_name": "Builder",
                                        "model": "m", "mode": "agent",
                                        **({"kind": "sub_agent"} if marker else {}),
                                        **({"workspace": workspace} if workspace else {}),
                                        **({"restart_resumes": restart_resumes} if restart_resumes else {})})
        else:
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


async def test_a_send_to_session_sub_agent_is_resumed_in_the_worker_chat(world):
    """The production shape: filed under the parent chat, worker chat in target_session."""
    old = world["crash"](subagent=True)

    result = await agent_control.recover_after_restart()
    await _settle()

    assert [r["run_id"] for r in result["resumed"]] == [old]
    assert len(world["calls"]) == 1
    context = world["calls"][0]
    assert any(SUBAGENT_MESSAGE in str(m.get("content")) for m in context), "the interrupted request is lost"
    assert f"bash: {COMMAND}" in context[-1]["content"]
    new = next(r for r in act.list_runs(session_id="w-1") if r["run_id"] != old)
    assert new["status"] == "completed" and new["summary"]["parent_session"] == "p-1"
    assert new["summary"]["restart_resumes"] == 1
    told = _to_parent(world)
    assert [m.content.split("]")[0] for m in told] == [
        "[Worker Builder was interrupted by an app restart and resumed", "[Worker Builder finished"]


async def test_a_sub_agent_whose_chat_saved_no_workspace_resumes_in_the_one_its_run_recorded(world, monkeypatch):
    """2026-10-11: the Lead Engineer sub-agent got its workspace from each
    send_to_session call and never had one saved on its chat. After a restart,
    recovery relaunched it with none and preflight refused: "Pass
    workspace=<one of: ...>; this chat has none". The run record knows where
    the worker was working; recovery uses that and saves it on the chat."""
    import core.database as database
    import src.tool_security as tool_security

    repo = world["tmp"].parent / (world["tmp"].name + "-repo")
    repo.mkdir()
    saved = {}
    monkeypatch.setattr(database, "update_session_settings",
                        lambda sid, patch, **kw: saved.setdefault(sid, {}).update(patch) or {})
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: True)
    old = world["crash"](subagent=True, marker=True, workspace=str(repo),
                         message="Fix the failing tests in the repository and push the branch")

    result = await agent_control.recover_after_restart()
    await _settle()

    assert [r["run_id"] for r in result["resumed"]] == [old], _to_parent(world)[-1].content[:300]
    new = next(r for r in act.list_runs(session_id="w-1") if r["run_id"] != old)
    assert new["summary"]["workspace"] == os.path.realpath(str(repo))
    assert saved["w-1"]["workspace"] == os.path.realpath(str(repo)), "the next resume would need the repair again"


async def test_a_sub_agent_past_the_limit_tells_the_parent_once(world):
    world["crash"](subagent=True, restart_resumes=2)

    await agent_control.recover_after_restart()
    await _settle()

    assert not world["calls"]
    told = _to_parent(world)
    assert len(told) == 1 and "not resumed" in told[0].content


async def test_parent_continuation_runs_are_not_mistaken_for_sub_agents(world):
    """_continue_parent files a run under the parent too, with the worker as target_session."""
    rid = act.run_started("p-1", "session", "Continuing after worker Builder", owner="alice",
                          data={"target_session": "w-1", "target_session_name": "Builder", "mode": "agent"})
    act.flush_runs()
    act._reset_for_tests()

    result = await agent_control.recover_after_restart()
    await _settle()

    assert result["resumed"] == [] and not world["calls"] and not _to_parent(world)
    assert act.get_run(rid)["status"] == "interrupted"


async def _held_worker(world):
    world["hold"] = asyncio.Event()
    started = await agent_control.launch_worker(owner="alice", task=TASK, parent_session="p-1", preflight=False)
    for _ in range(200):
        if world["calls"]:
            break
        await asyncio.sleep(0.01)
    assert world["calls"], "the worker never reached its model"
    return started["run_id"]


async def test_graceful_shutdown_marks_a_running_worker_interrupted_and_startup_resumes_it(world):
    run_id = await _held_worker(world)
    act.note_progress(run_id, current_tool="bash", current_command=COMMAND)

    assert await agent_control.interrupt_workers_for_shutdown() == 1
    await asyncio.wait_for(asyncio.gather(*agent_control._WORKERS.values()), timeout=5) if agent_control._WORKERS else None

    rec = act.get_run(run_id)
    assert rec["status"] == "interrupted"
    assert "the app shut down" in rec["summary"]["interrupted_reason"] and COMMAND in rec["summary"]["interrupted_reason"]
    assert not _to_parent(world), "shutdown must not hand the parent a 'stopped' result"

    act._reset_for_tests()  # the restart
    world["hold"] = None
    world["calls"].clear()
    result = await agent_control.recover_after_restart()
    await _settle()

    assert [r["run_id"] for r in result["resumed"]] == [run_id]
    assert f"bash: {COMMAND}" in world["calls"][0][-1]["content"]


async def test_a_user_stop_stays_cancelled_and_is_not_resumed(world):
    run_id = await _held_worker(world)
    assert (await agent_control.stop_run(run_id, by="from the Workbench"))["stopped"]
    await asyncio.wait_for(agent_control._WORKERS[run_id], timeout=5) if run_id in agent_control._WORKERS else None
    assert act.get_run(run_id)["status"] == "cancelled"
    before = len(world["calls"])

    act._reset_for_tests()  # restart
    result = await agent_control.recover_after_restart()
    await _settle()

    assert result["resumed"] == [] and len(world["calls"]) == before
    assert act.get_run(run_id)["status"] == "cancelled"


async def test_the_explicit_marker_identifies_a_sub_agent_whatever_its_title(world):
    """The title is only a fallback for runs recorded before the marker existed."""
    old = world["crash"](subagent=True, marker=True, title="Delegated: carry on")

    result = await agent_control.recover_after_restart()
    await _settle()

    assert [r["run_id"] for r in result["resumed"]] == [old]


async def test_a_run_with_another_kind_is_not_taken_for_a_sub_agent_by_its_title(world):
    rid = act.run_started("p-1", "session", "Sub-agent · look-alike", owner="alice",
                          data={"target_session": "w-1", "mode": "agent", "kind": "continuation"})
    act.flush_runs()
    act._reset_for_tests()

    result = await agent_control.recover_after_restart()
    await _settle()

    assert result["resumed"] == [] and act.get_run(rid)["status"] == "interrupted"
