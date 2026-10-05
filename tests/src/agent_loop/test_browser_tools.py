"""A turn routed to the browser gets the browser tools the server really has.

The chat route only says "browser" (the ``builtin_browser`` sentinel); the
agent loop swaps that for every tool the connected Playwright MCP server
lists. Playwright renames its tools between releases, so a hardcoded list
would offer the model names the server no longer has. The browser catalog is
gated (sent only when selected), so for a model with native tool calling the
sentinel's expansion is the only way those schemas reach the request.
"""
import asyncio
import json

import src.agent_loop as al
from src.mcp_manager import McpManager

# Names a hardcoded list would not know.
SERVER_TOOLS = ("browser_snapshot", "browser_mouse_down", "browser_tabs_v2")


def test_the_browser_sentinel_offers_every_tool_the_browser_server_lists(monkeypatch):
    manager = McpManager()
    manager._connections["builtin_browser"] = {"status": "connected", "name": "Built-in: Browser"}
    manager._tools["builtin_browser"] = [
        {"name": name, "description": name, "input_schema": {"type": "object", "properties": {}}}
        for name in SERVER_TOOLS
    ]
    offered = []

    async def fake_model(_candidates, messages, **kwargs):
        offered.append({(t.get("function") or t).get("name") for t in (kwargs.get("tools") or [])})
        yield f'data: {json.dumps({"delta": "Done."})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_model)
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: manager)
    # An admin's turn: a public user's turn gets no MCP tools at all.
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set())

    async def run():
        loop = al.stream_agent_loop(
            # qwen3 models call tools natively, so the schemas go in `tools`.
            "http://model.test/v1", "qwen3-8b",
            [{"role": "user", "content": "open example.com in the browser and read me the headline"}],
            relevant_tools={"builtin_browser"},
        )
        return [chunk async for chunk in loop]

    asyncio.run(run())

    assert offered, "the loop never called the model"
    assert {f"mcp__builtin_browser__{name}" for name in SERVER_TOOLS} <= offered[0]
