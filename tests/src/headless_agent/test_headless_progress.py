"""A detached run's live counters reach its run record, not just the log.

A worker ran 108 rounds / ~23 minutes while the Workbench showed only its last
event title; the round, token totals and cache hit rate were in the server log
alone. The headless drain now folds the loop's frames into the run's
``progress`` (agent_activity.note_progress), which /api/workbench/runs returns.
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


def test_drain_tracks_round_tools_and_token_totals(data_dir, monkeypatch):
    import src.agent_loop as agent_loop
    import src.model_context as model_context
    from core.models import Session
    from src.headless_agent import run_headless

    seen_mid_tool = {}

    async def fake_loop(url, model, messages, **kwargs):
        yield _frame({"type": "round_usage", "round": 1, "input": 10_000, "cached": 0, "output": 200})
        yield _frame({"type": "tool_start", "tool": "read_file", "command": "src/a.py"})
        # While the tool runs, the row names it.
        seen_mid_tool.update(act.get_run(rid)["progress"])
        yield _frame({"type": "tool_output", "tool": "read_file", "output": "ok", "exit_code": 0})
        yield _frame({"type": "agent_step", "round": 2})
        yield _frame({"type": "round_usage", "round": 2, "input": 12_000, "cached": 9_000, "output": 300})
        yield _frame({"type": "tool_start", "tool": "bash", "command": "ls"})
        yield _frame({"type": "tool_output", "tool": "bash", "output": "a.py", "exit_code": 0})
        yield _frame({"delta": "Done."})
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop, raising=False)
    monkeypatch.setattr(model_context, "get_context_length", lambda url, model: 32_000)
    sess = Session(id="w-prog", name="worker", endpoint_url="http://llm.test/v1", model="m")
    rid = act.run_started(sess.id, "session", "Worker: read things")

    text, events = asyncio.run(run_headless(sess, [{"role": "user", "content": "go"}],
                                            activity_session_id=sess.id, run_id=rid))

    assert text == "Done." and len(events) == 2
    assert seen_mid_tool["current_tool"] == "read_file"
    progress = act.list_runs(session_id=sess.id)[0]["progress"]
    assert progress["round"] == 2
    assert progress["tool_calls"] == 2
    assert progress["current_tool"] is None
    assert (progress["input_tokens"], progress["cached_tokens"], progress["output_tokens"]) == (22_000, 9_000, 500)
    assert progress["last_event_at"] > 0
    # The counters never went through the event ring.
    assert not [e for e in act.run_events(rid) if (e.get("data") or {}).get("input_tokens")]
