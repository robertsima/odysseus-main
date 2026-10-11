"""A run found live after a restart says why it ended, and what it was doing.

2026-10-10: the app was hard-killed twice under a worker running a bash
command. The registry flipped the worker to "interrupted" with no reason, so
nobody could tell the restart from a failure, or which command to avoid.
"""
import json
import os

import pytest

from src import agent_activity as act
from src import constants


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    act._reset_for_tests()
    yield tmp_path
    act._reset_for_tests()


def _fresh_process():
    """Drop in-memory state the way a restart does; the runs file stays."""
    act._reset_for_tests()


def test_live_run_found_after_restart_is_interrupted_with_the_command_it_was_running(data_dir):
    rid = act.run_started("w-1", "session", "Worker: build", data={"parent_session": "p-1"})
    act.note_progress(rid, current_tool="bash", current_target="python -m pytest tests -q",
                      current_started_at=1000.0)

    _fresh_process()
    reconciled = act.reconcile_interrupted()

    rec = act.get_run(rid)
    assert rec["status"] == "interrupted" and rec["finished_at"]
    reason = rec["summary"]["interrupted_reason"]
    assert "restart" in reason and "bash" in reason and "python -m pytest tests -q" in reason
    assert [r["run_id"] for r in reconciled] == [rid]


def test_reason_falls_back_to_the_unfinished_tool_event_when_progress_has_no_tool(data_dir):
    rid = act.run_started("w-2", "session", "Worker: build")
    act.publish("w-2", "tool_start", "bash make -j64 all", source="session", run_id=rid)

    _fresh_process()
    act.reconcile_interrupted()

    assert "bash make -j64 all" in act.get_run(rid)["summary"]["interrupted_reason"]


def test_finished_tool_is_not_named_as_the_last_activity(data_dir):
    rid = act.run_started("w-3", "session", "Worker: build")
    act.publish("w-3", "tool_start", "bash ls", source="session", run_id=rid)
    act.publish("w-3", "tool_result", "bash done", source="session", run_id=rid)

    _fresh_process()
    act.reconcile_interrupted()

    reason = act.get_run(rid)["summary"]["interrupted_reason"]
    assert "restart" in reason and "bash ls" not in reason


def test_reconcile_is_written_to_disk_so_a_second_restart_does_not_repeat_it(data_dir):
    rid = act.run_started("w-4", "session", "Worker: build")
    _fresh_process()
    assert [r["run_id"] for r in act.reconcile_interrupted()] == [rid]

    _fresh_process()
    assert act.reconcile_interrupted() == []
    on_disk = json.load(open(os.path.join(str(data_dir), "agent_activity", "runs.json"), encoding="utf-8"))
    assert on_disk[rid]["status"] == "interrupted"


def test_mid_tool_state_reaches_disk_before_the_process_can_die(data_dir, monkeypatch):
    """note_progress saves every 5 s; a shell command starting inside that window was never on disk."""
    import asyncio

    import src.agent_loop as agent_loop
    import src.model_context as model_context
    from core.models import Session
    from src.headless_agent import run_headless

    seen = {}

    def frame(payload):
        return f"data: {json.dumps(payload)}\n\n"

    async def fake_loop(url, model, messages, **kwargs):
        yield frame({"type": "round_usage", "round": 1, "input": 10, "cached": 0, "output": 1})
        act.flush_runs()  # the debounced writer has run; the next save is 5 s away
        yield frame({"type": "tool_start", "tool": "bash", "command": json.dumps({"command": "pytest -q"})})
        act.flush_runs()
        with open(os.path.join(str(data_dir), "agent_activity", "runs.json"), encoding="utf-8") as fh:
            seen.update(json.load(fh)[rid].get("progress") or {})
        yield frame({"type": "tool_output", "tool": "bash", "output": "ok", "exit_code": 0})
        yield frame({"delta": "Done."})
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop, raising=False)
    monkeypatch.setattr(model_context, "get_context_length", lambda url, model: 32_000)
    sess = Session(id="w-5", name="worker", endpoint_url="http://llm.test/v1", model="m")
    rid = act.run_started(sess.id, "session", "Worker: test")

    asyncio.run(run_headless(sess, [{"role": "user", "content": "go"}], activity_session_id=sess.id, run_id=rid))

    assert seen.get("current_tool") == "bash"
    assert seen.get("current_command") == "pytest -q"
    assert seen.get("current_started_at")
