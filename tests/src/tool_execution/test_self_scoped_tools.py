"""A selected-tools agent may still run the tools that touch only its own state.

The prompt offers update_plan and the recall tools to an agent whose loadout
lists its tools, even when the list predates them. Execution must admit what
the offer admits, while still refusing other tools the list leaves out.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

import core.database
from src import tool_execution

pytestmark = pytest.mark.security

LOADOUT = {"tool_access": "selected", "enabled_tools": ["bash", "read_file"]}


@pytest.fixture
def selected_tools_chat(monkeypatch):
    # With auth off the caller is the single local user; only the loadout decides.
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(core.database, "get_session_settings", lambda session_id, strict=False: dict(LOADOUT))


def _run(tool, content):
    block = SimpleNamespace(tool_type=tool, content=content)
    _, result = asyncio.run(tool_execution.execute_tool_block(
        block, session_id="s1", security_context=tool_execution.NO_TOOL_SECURITY_CONTEXT,
    ))
    return result


def test_the_task_checklist_runs_for_a_selected_tools_agent(selected_tools_chat):
    result = _run("update_plan", json.dumps({"steps": [{"step": "write the test", "status": "in_progress"}]}))

    assert "tool allowlist" not in json.dumps(result)


def test_a_tool_the_list_leaves_out_is_still_refused(selected_tools_chat):
    result = _run("web_search", "odysseus")

    assert "tool allowlist" in result.get("error", "")
