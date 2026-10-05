"""The loop offloads an oversized tool result before it joins the history.

MCP output has no size cap of its own. Replayed on every later round, one big
result made a GitHub research turn grow to 700k tokens.
"""
import asyncio
import json

import src.agent_loop as al
from src import constants

BIG_OUTPUT = "\n".join(f"line {i}: something happened in the build" for i in range(6000))
CALL = {"id": "call_big", "name": "update_plan", "arguments": json.dumps({"plan": "- [ ] read the log"})}


def _run(monkeypatch, tmp_path):
    """Round 1 calls a tool that returns BIG_OUTPUT. Returns the (messages, kwargs) of each round."""
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    seen = []

    async def fake_model(_candidates, messages, **kwargs):
        seen.append(([dict(m) for m in messages], kwargs))
        if len(seen) == 1:
            yield f'data: {json.dumps({"type": "tool_calls", "calls": [CALL]})}\n\n'
        else:
            yield f'data: {json.dumps({"delta": "Done."})}\n\n'
        yield "data: [DONE]\n\n"

    async def fake_tool(block, *args, **kwargs):
        return block.tool_type, {"output": BIG_OUTPUT, "exit_code": 0}

    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_model)
    monkeypatch.setattr(al, "execute_tool_block", fake_tool)
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None)

    async def run():
        loop = al.stream_agent_loop(
            "http://model.test/v1", "m",
            [{"role": "user", "content": "read the build log"}],
            relevant_tools={"update_plan"},
        )
        return [chunk async for chunk in loop]

    asyncio.run(run())
    assert len(seen) == 2
    return seen


def test_an_oversized_result_reaches_the_model_as_an_excerpt_with_a_ref(monkeypatch, tmp_path):
    _first, (messages, _kwargs) = _run(monkeypatch, tmp_path)

    [tool_message] = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_message["content"]) < len(BIG_OUTPUT) / 4
    assert "recall_tool_output" in tool_message["content"]
