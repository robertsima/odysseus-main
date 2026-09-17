"""Authorization and routing boundaries for the dedicated Git tool."""

import json
from unittest.mock import AsyncMock

import pytest

from src.agent_tools.git_tools import GitTool, _write_token
from src import tool_approvals, tool_execution
from src.tool_types import ToolBlock
from src.tool_parsing import parse_tool_blocks


@pytest.fixture(autouse=True)
def reset_approvals():
    tool_approvals._reset_for_tests()
    yield
    tool_approvals._reset_for_tests()


def _admin(monkeypatch, allowed=True):
    monkeypatch.setattr(
        "src.tool_security.owner_is_admin_or_single_user", lambda owner: allowed
    )
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: allowed)
    monkeypatch.setattr(
        "src.tool_security.owner_baseline_disabled_tools", lambda owner: set()
    )
    monkeypatch.setattr("src.settings.load_disabled_tools_strict", lambda: [])


@pytest.mark.asyncio
async def test_direct_handler_is_admin_only(monkeypatch):
    _admin(monkeypatch, False)
    result = await GitTool().execute('{"action":"repositories"}', {"owner": "member"})
    assert result["code"] == "admin_required"


@pytest.mark.asyncio
async def test_routine_action_dispatches_without_approval(monkeypatch):
    _admin(monkeypatch)
    implementation = AsyncMock(return_value={"clean": True})
    monkeypatch.setattr(
        "src.agent_worktree.repository_local.execute_local", implementation
    )
    content = json.dumps({"action": "status", "repository": "/repos/app"})
    result = await GitTool().execute(content, {"owner": "admin", "session_id": "s1"})
    assert result == {"exit_code": 0, "result": {"clean": True}}
    implementation.assert_awaited_once_with("status", "/repos/app")


@pytest.mark.asyncio
async def test_routine_ask_all_once_grant_is_consumed_by_handler(monkeypatch):
    _admin(monkeypatch)
    implementation = AsyncMock(return_value={"paths": ["README.md"]})
    monkeypatch.setattr(
        "src.agent_worktree.repository_local.execute_local", implementation
    )
    content = json.dumps(
        {"action": "stage", "repository": "/repos/app", "paths": ["README.md"]}
    )
    pending = tool_approvals.request("s1", "manage_git", content, "changes files")
    tool_approvals.decide("s1", pending["id"], "once")
    assert tool_approvals.has_once_grant("s1", "manage_git", content)
    assert (await GitTool().execute(content, {"owner": "admin", "session_id": "s1"}))[
        "exit_code"
    ] == 0
    assert not tool_approvals.has_once_grant("s1", "manage_git", content)


@pytest.mark.asyncio
async def test_risky_action_requires_session_exact_once_grant_and_revision_proof(
    monkeypatch,
):
    _admin(monkeypatch)
    implementation = AsyncMock(return_value={"updated": True})
    monkeypatch.setattr(
        "src.agent_worktree.repository_remote.execute_remote", implementation
    )
    args = {
        "action": "merge",
        "repository": "/repos/app",
        "ref": "topic",
        "expected_head": "a" * 40,
        "expected_target": "b" * 40,
    }
    content = json.dumps(args)
    no_session = await GitTool().execute(content, {"owner": "admin"})
    assert no_session["code"] == "approval_required"
    missing = dict(args)
    missing.pop("expected_target")
    assert (
        await GitTool().execute(
            json.dumps(missing), {"owner": "admin", "session_id": "s1"}
        )
    )["code"] == "missing_revision"
    pending = tool_approvals.request("s1", "manage_git", content, "merge")
    assert tool_approvals.decide("s1", pending["id"], "once")["decision"] == "once"
    assert (await GitTool().execute(content, {"owner": "admin", "session_id": "s1"}))[
        "exit_code"
    ] == 0
    assert (await GitTool().execute(content, {"owner": "admin", "session_id": "s1"}))[
        "code"
    ] == "approval_required"


@pytest.mark.asyncio
async def test_grant_is_not_canonicalized_across_whitespace_in_path(monkeypatch):
    _admin(monkeypatch)
    base = {"action": "push", "repository": "/repos/app", "expected_head": "a" * 40}
    content = json.dumps(base)
    pending = tool_approvals.request("s1", "manage_git", content, "push")
    tool_approvals.decide("s1", pending["id"], "once")
    changed = json.dumps({**base, "repository": "/repos/app "})
    result = await GitTool().execute(changed, {"owner": "admin", "session_id": "s1"})
    assert result["code"] == "approval_required"
    assert tool_approvals.has_once_grant("s1", "manage_git", content)


def test_manage_git_never_creates_broad_always_permission():
    content = json.dumps(
        {"action": "push", "repository": "/r", "expected_head": "a" * 40}
    )
    pending = tool_approvals.request("s1", "manage_git", content, "push")
    decided = tool_approvals.decide("s1", pending["id"], "always")
    assert decided["decision"] == "once"
    assert "manage_git" not in tool_approvals.chat_grants("s1")


@pytest.mark.asyncio
async def test_model_supplied_approval_parameter_is_rejected(monkeypatch):
    _admin(monkeypatch)
    result = await GitTool().execute(
        json.dumps(
            {
                "action": "push",
                "repository": "/repos/app",
                "expected_head": "a" * 40,
                "approved": True,
            }
        ),
        {"owner": "admin", "session_id": "s1"},
    )
    assert result["code"] == "invalid_arguments"


@pytest.mark.parametrize(
    "settings,enabled,expected",
    [
        ({"allowed_mcp_servers": ["github_write"]}, "true", "secret"),
        ({"allowed_mcp_servers": []}, "true", None),
        ({"allowed_mcp_servers": ["github_read"]}, "true", None),
        ({"allowed_mcp_servers": ["github_write"]}, "false", None),
    ],
)
def test_push_token_requires_write_opt_in_and_profile_ceiling(
    monkeypatch, settings, enabled, expected
):
    monkeypatch.setattr(
        "core.database.get_session_settings", lambda sid, strict=False: settings
    )
    monkeypatch.setenv("ODYSSEUS_GITHUB_MCP_WRITE", enabled)
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "secret")
    monkeypatch.setenv("GITHUB_HOST", "github.com")
    assert _write_token({"session_id": "s1"}) == expected
    assert _write_token({}) is None


@pytest.mark.asyncio
async def test_dispatcher_enforces_profile_and_active_revocation(monkeypatch):
    _admin(monkeypatch)
    handler = AsyncMock(return_value={"exit_code": 0})
    monkeypatch.setitem(
        __import__("src.agent_tools", fromlist=["TOOL_HANDLERS"]).TOOL_HANDLERS,
        "manage_git",
        handler,
    )
    block = ToolBlock("manage_git", '{"action":"repositories"}')
    monkeypatch.setattr(
        "core.database.get_session_settings",
        lambda *a, **k: {"tool_access": "selected", "enabled_tools": ["read_file"]},
    )
    _, denied = await tool_execution.execute_tool_block(
        block, session_id="s1", owner="admin"
    )
    assert denied["exit_code"] == 1 and "selected tool bindings" in denied["error"]
    monkeypatch.setattr("core.database.get_session_settings", lambda *a, **k: {})
    _, revoked = await tool_execution.execute_tool_block(
        block, session_id="s1", owner="admin", disabled_tools={"manage_git"}
    )
    assert revoked["exit_code"] == 1 and "disabled by user" in revoked["error"]
    handler.assert_not_awaited()


def test_risky_approval_overrides_auto_and_fenced_routing_is_exact():
    risky = json.dumps(
        {
            "action": "delete_branch",
            "repository": "/r",
            "name": "old",
            "expected_head": "a" * 40,
            "expected_target": "b" * 40,
        }
    )
    assert "deletes" in tool_approvals.approval_reason("manage_git", risky, "auto")
    assert (
        tool_approvals.approval_reason(
            "manage_git", '{"action":"status","repository":"/r"}', "auto"
        )
        is None
    )
    blocks = parse_tool_blocks(f"```manage_git\n{risky}\n```")
    assert [(b.tool_type, b.content) for b in blocks] == [("manage_git", risky)]


def test_native_routing_preserves_exact_manage_git_arguments():
    from src.agent_loop import _resolve_tool_blocks

    arguments = json.dumps({"action": "status", "repository": "/repos/app"})
    blocks, used_native, converted = _resolve_tool_blocks(
        "",
        [{"id": "call-1", "name": "manage_git", "arguments": arguments}],
        1,
        is_api_model=True,
    )
    assert used_native is True
    assert converted[0]["id"] == "call-1"
    assert [(b.tool_type, json.loads(b.content)) for b in blocks] == [
        ("manage_git", {"action": "status", "repository": "/repos/app"})
    ]
