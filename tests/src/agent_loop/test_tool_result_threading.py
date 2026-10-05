"""Each tool result goes back to the model under the id of the call it answers.

A native call whose arguments cannot be parsed is dropped before execution,
so the loop runs fewer tools than the model asked for. The results that come
back must still line up with the calls that ran: a result filed under another
call's tool_call_id answers the wrong question, and the call that did run
looks unanswered.
"""
import asyncio
import json

import pytest

import src.agent_loop as al

pytestmark = pytest.mark.security

BROKEN_CALL = {"id": "call_broken", "name": "update_plan", "arguments": '{"plan": "- [ ] cut off'}
PLAN_CALL = {"id": "call_plan", "name": "update_plan", "arguments": json.dumps({"plan": "- [ ] step one"})}


def _run_two_rounds(monkeypatch):
    """Round 1 asks for both calls, round 2 answers. Returns round 2's messages."""
    seen = []

    async def fake_model(_candidates, messages, **kwargs):
        seen.append([dict(m) for m in messages])
        if len(seen) == 1:
            yield f'data: {json.dumps({"type": "tool_calls", "calls": [BROKEN_CALL, PLAN_CALL]})}\n\n'
        else:
            yield f'data: {json.dumps({"delta": "Done."})}\n\n'
        yield "data: [DONE]\n\n"

    async def fake_tool(block, *args, **kwargs):
        return block.tool_type, {"output": "plan saved", "exit_code": 0}

    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_model)
    monkeypatch.setattr(al, "execute_tool_block", fake_tool)
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None)

    async def run():
        loop = al.stream_agent_loop(
            "http://model.test/v1", "m",
            [{"role": "user", "content": "make a plan"}],
            relevant_tools={"update_plan"},
        )
        return [chunk async for chunk in loop]

    asyncio.run(run())
    assert len(seen) == 2, "the loop did not go back to the model after the tool ran"
    return seen[1]


def test_the_tool_result_carries_the_id_of_the_call_that_ran(monkeypatch):
    messages = _run_two_rounds(monkeypatch)

    answers = {m["tool_call_id"]: m["content"] for m in messages if m.get("role") == "tool"}
    assert "plan saved" in str(answers.get("call_plan", ""))
    assert "plan saved" not in str(answers.get("call_broken", ""))
