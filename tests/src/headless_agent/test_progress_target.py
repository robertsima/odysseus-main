"""A run's live progress names what its current tool works on, and a resumed
run clears the "resuming" note.

The agent strip showed "round 74 · read_file" with no file, and a worker
waiting to resume after a provider error looked stuck (2026-10-08).
"""
import asyncio
import json

import pytest

from src import agent_activity as act
from src import constants


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    act._reset_for_tests()
    yield tmp_path
    act._reset_for_tests()


def _frame(payload):
    return f"data: {json.dumps(payload)}\n\n"


def _drain(monkeypatch, rid, frames, seen):
    import src.agent_loop as agent_loop
    import src.model_context as model_context
    from core.models import Session
    from src.headless_agent import run_headless

    async def fake_loop(url, model, messages, **kwargs):
        for frame in frames:
            if frame == "snapshot":
                seen.append(dict(act.get_run(rid)["progress"]))
                continue
            yield _frame(frame)
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop, raising=False)
    monkeypatch.setattr(model_context, "get_context_length", lambda url, model: 32_000)
    sess = Session(id="w-target", name="worker", endpoint_url="http://llm.test/v1", model="m")
    asyncio.run(run_headless(sess, [{"role": "user", "content": "go"}], activity_session_id=sess.id, run_id=rid))


def test_the_current_tool_carries_its_target(data_dir, monkeypatch):
    rid = act.run_started("w-target", "session", "Worker")
    seen = []
    _drain(monkeypatch, rid, [
        {"type": "tool_start", "tool": "read_file", "command": json.dumps({"path": "static/js/theme.js"})},
        "snapshot",
        {"type": "tool_output", "tool": "read_file", "output": "ok", "exit_code": 0},
        "snapshot",
    ], seen)

    assert seen[0]["current_tool"] == "read_file"
    assert seen[0]["current_target"] == "static/js/theme.js"
    assert seen[1]["current_target"] is None


def test_a_resumed_run_clears_the_waiting_note(data_dir, monkeypatch):
    rid = act.run_started("w-target", "session", "Worker")
    act.note_progress(rid, waiting="Upstream model error; resuming in 30s (1/2)")
    seen = []
    _drain(monkeypatch, rid, [{"type": "agent_step", "round": 2}, "snapshot"], seen)

    assert seen[0]["waiting"] is None
