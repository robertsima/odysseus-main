"""A steer meets a turn that ends on purpose, or a wait it would have to sit through.

* When the turn ends before the loop reads the steer queue (the tool budget was
  reached, the agent asked a question), the dropped message is flagged `carry`
  with the reason, so the client sends it as the next message instead of
  returning it to the composer.
* A call that only waits is skipped while a steer is pending; the user's
  message is not held for a second wait.
"""
import asyncio
import json

import pytest

import src.agent_loop as al
from src import agent_activity, agent_control, constants

PLAN_CALL = {"id": "call_plan", "name": "update_plan", "arguments": json.dumps({"plan": "- [ ] step"})}
SECOND_CALL = {"id": "call_two", "name": "update_plan", "arguments": json.dumps({"plan": "- [ ] more"})}
WAIT_CALL = {
    "id": "call_wait", "name": "manage_agent_loadout",
    "arguments": json.dumps({"action": "wait", "wait_seconds": 600}),
}


@pytest.fixture(autouse=True)
def _isolated_steer_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    agent_activity._reset_for_tests()
    agent_control._STEER.clear()
    yield
    agent_control._STEER.clear()
    agent_activity._reset_for_tests()


def _run(monkeypatch, calls, tool_result, queue_steer=True, **loop_args):
    """Round 1 asks for `calls`; the first call to run queues a steer. Returns (frames, executed tools)."""
    executed = []
    rounds = []

    async def fake_model(_candidates, messages, **kwargs):
        rounds.append(1)
        if len(rounds) == 1:
            yield f'data: {json.dumps({"type": "tool_calls", "calls": calls})}\n\n'
        else:
            yield f'data: {json.dumps({"delta": "Done."})}\n\n'
        yield "data: [DONE]\n\n"

    async def fake_tool(block, *args, **kwargs):
        executed.append(block.tool_type)
        if len(executed) == 1 and queue_steer:
            agent_control.steer("s", "actually, use the archive", run_id="r1")
        return block.tool_type, dict(tool_result)

    monkeypatch.setenv("AUTH_ENABLED", "false")  # single local user: admin tools are not blocked
    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_model)
    monkeypatch.setattr(al, "execute_tool_block", fake_tool)
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: [] if key == "disabled_tools" else default)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None)

    async def run():
        loop = al.stream_agent_loop(
            "http://model.test/v1", "m",
            [{"role": "user", "content": "make a plan, then use manage_agent_loadout to start a worker agent and wait for it"}],
            session_id="s", steer_run_id="r1",
            relevant_tools={"update_plan", "manage_agent_loadout"},
            **loop_args,
        )
        return [chunk async for chunk in loop]

    frames = asyncio.run(run())
    return frames, executed


def _dropped(frames):
    [frame] = [f for f in frames if "steer_dropped" in f]
    return json.loads(frame[len("data: "):])


def test_a_steer_left_by_the_tool_budget_is_carried_to_the_next_message(monkeypatch):
    frames, _executed = _run(monkeypatch, [PLAN_CALL, SECOND_CALL], {"output": "ok", "exit_code": 0},
                             max_tool_calls=1)

    dropped = _dropped(frames)
    assert dropped["carry"] is True
    assert dropped["reason"] == "the tool budget was reached"
    assert [m["text"] for m in dropped["messages"]] == ["actually, use the archive"]


def test_a_steer_left_by_a_question_to_the_user_is_carried_to_the_next_message(monkeypatch):
    ask = {"ask_user": {"question": "Which folder?", "options": ["a", "b"]}, "output": "asked", "exit_code": 0}

    frames, _executed = _run(monkeypatch, [PLAN_CALL], ask)

    dropped = _dropped(frames)
    assert dropped["carry"] is True
    assert dropped["reason"] == "the agent asked you a question"


def test_a_wait_only_call_is_skipped_while_a_steer_is_pending(monkeypatch):
    _frames, executed = _run(monkeypatch, [PLAN_CALL, WAIT_CALL], {"output": "ok", "exit_code": 0})

    assert executed == ["update_plan"]


def test_the_same_wait_runs_when_no_steer_is_pending(monkeypatch):
    _frames, executed = _run(monkeypatch, [PLAN_CALL, WAIT_CALL], {"output": "ok", "exit_code": 0},
                             queue_steer=False)

    assert executed == ["update_plan", "manage_agent_loadout"]
