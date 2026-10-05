"""Session and model tools run through the tool registry, not the older AI dispatcher.

The older dispatcher knows neither the caller's owner nor the registry's
handler context, so a tool that falls back to it runs unscoped or not at all.
"""
import asyncio
from types import SimpleNamespace

import pytest

from src import ai_interaction, tool_execution

REGISTRY_TOOLS = (
    "create_session", "list_sessions", "send_to_session", "manage_session",
    "chat_with_model", "ask_teacher", "list_models",
)


@pytest.mark.parametrize("tool", REGISTRY_TOOLS)
def test_tool_is_dispatched_through_the_registry(monkeypatch, tool):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    registry_calls = []

    async def registry(name, *args, **kwargs):
        registry_calls.append(name)
        return {"results": "ok"}

    async def older_dispatcher(name, *args, **kwargs):
        return name, {"error": "older dispatcher reached", "exit_code": 1}

    monkeypatch.setattr(tool_execution, "_document_tool_dispatch", registry)
    monkeypatch.setattr(ai_interaction, "dispatch_ai_tool", older_dispatcher)
    block = SimpleNamespace(tool_type=tool, content="x")

    _, result = asyncio.run(tool_execution.execute_tool_block(
        block, session_id="s1", security_context=tool_execution.NO_TOOL_SECURITY_CONTEXT,
    ))

    assert registry_calls == [tool]
    assert result == {"results": "ok"}
