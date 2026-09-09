"""The two new tools must be reachable, admin-gated, and correctly plan-moded.

A tool that is dispatchable but not in the policy sets is a privilege hole; a
tool that is in the policy sets but not dispatchable is dead code. Both
directions are asserted here.
"""

import pytest

from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
from src.tool_execution import _ADMIN_TOOLS
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.tool_security import (
    NON_ADMIN_BLOCKED_TOOLS,
    PLAN_MODE_READONLY_TOOLS,
    is_public_blocked_tool,
    plan_mode_disabled_tools,
)

pytestmark = pytest.mark.area_security

NEW_TOOLS = ("manage_agent_worktree", "read_app_logs")


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_tool_is_dispatchable(name):
    assert name in TOOL_HANDLERS
    assert name in TOOL_TAGS


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_tool_has_a_native_function_schema(name):
    names = {(t.get("function") or {}).get("name") for t in FUNCTION_TOOL_SCHEMAS}
    assert name in names


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_tool_is_admin_only(name):
    assert name in _ADMIN_TOOLS
    assert name in NON_ADMIN_BLOCKED_TOOLS
    assert is_public_blocked_tool(name) is True


def test_worktree_tool_is_blocked_in_plan_mode():
    assert "manage_agent_worktree" in plan_mode_disabled_tools()
    assert "manage_agent_worktree" not in PLAN_MODE_READONLY_TOOLS


def test_log_reading_is_allowed_in_plan_mode():
    assert "read_app_logs" in PLAN_MODE_READONLY_TOOLS
    assert "read_app_logs" not in plan_mode_disabled_tools()


async def test_worktree_tool_rejects_an_unknown_action():
    result = await TOOL_HANDLERS["manage_agent_worktree"]('{"action": "push_to_main"}', {})
    assert result["exit_code"] == 1
    assert "unknown action" in result["error"]


async def test_worktree_tool_rejects_malformed_arguments():
    result = await TOOL_HANDLERS["manage_agent_worktree"]("{not json", {})
    assert result["exit_code"] == 1
    assert "invalid JSON" in result["error"]


async def test_publish_action_requires_a_code_from_a_human():
    result = await TOOL_HANDLERS["manage_agent_worktree"](
        '{"action": "publish", "request_id": "abc"}', {}
    )
    assert result["exit_code"] == 1
    assert "approval_code" in result["error"]


async def test_the_tool_exposes_no_way_to_self_approve():
    """There is deliberately no 'approve' action on the agent-facing tool."""
    result = await TOOL_HANDLERS["manage_agent_worktree"]('{"action": "approve"}', {})
    assert result["exit_code"] == 1
    assert "unknown action" in result["error"]


async def test_status_reports_publishing_disabled_by_default(monkeypatch, tmp_path):
    for name in ("ODYSSEUS_AGENT_PUBLISH_ENABLED", "ODYSSEUS_AGENT_REPO"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ODYSSEUS_AGENT_WORKTREE_ROOT", str(tmp_path / "wt"))
    monkeypatch.setenv("ODYSSEUS_AGENT_STATE_DIR", str(tmp_path / "state"))
    result = await TOOL_HANDLERS["manage_agent_worktree"]('{"action": "status"}', {})
    assert result["exit_code"] == 0
    assert result["status"]["publish_enabled"] is False
    assert result["status"]["publish_blockers"]


async def test_log_tool_lists_and_tails(monkeypatch, tmp_path):
    from src import agent_logs

    directory = tmp_path / "logs"
    directory.mkdir()
    (directory / "app.log").write_text("2026-01-01 - x - ERROR - boom\n")
    monkeypatch.setattr(agent_logs, "log_roots", lambda: [str(directory)])

    listing = await TOOL_HANDLERS["read_app_logs"]('{"action": "list"}', {})
    assert listing["exit_code"] == 0
    assert listing["logs"][0]["name"] == "app.log"

    tail = await TOOL_HANDLERS["read_app_logs"]('{"action": "tail", "level": "ERROR"}', {})
    assert tail["exit_code"] == 0
    assert "boom" in tail["output"]


async def test_log_tool_rejects_an_unknown_action():
    result = await TOOL_HANDLERS["read_app_logs"]('{"action": "delete"}', {})
    assert result["exit_code"] == 1
    assert "unknown action" in result["error"]
