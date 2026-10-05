"""Authorization and routing boundaries for the dedicated Git tool."""

import json
from unittest.mock import AsyncMock

import pytest

from src.agent_tools.git_tools import GitTool, _write_token
from src import tool_execution
from src.tool_types import ToolBlock


def _no_security_context():
    # Looked up at call time: other tests reload src.tool_execution, which
    # replaces the sentinel execute_tool_block compares by identity.
    import src.tool_execution as tool_execution

    return tool_execution.NO_TOOL_SECURITY_CONTEXT


@pytest.fixture(autouse=True)
def reset_approvals():
    yield


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
        ({"allowed_mcp_servers": ["github_write"]}, "true", "ghp_test_secret"),
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
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_test_secret")
    monkeypatch.setenv("GITHUB_HOST", "github.com")
    assert _write_token({"session_id": "s1"}) == expected
    assert _write_token({}) is None


def test_push_token_rejects_cross_service_secret(monkeypatch):
    monkeypatch.setattr(
        "core.database.get_session_settings",
        lambda *_a, **_k: {"allowed_mcp_servers": ["github_write"]},
    )
    monkeypatch.setenv("ODYSSEUS_GITHUB_MCP_WRITE", "1")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "ody_not_a_github_token")
    monkeypatch.delenv("GITHUB_HOST", raising=False)
    assert _write_token({"session_id": "s1"}) is None


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
        block, session_id="s1", owner="admin", security_context=_no_security_context()
    )
    assert denied["exit_code"] == 1 and "selected tool bindings" in denied["error"]
    monkeypatch.setattr("core.database.get_session_settings", lambda *a, **k: {})
    _, revoked = await tool_execution.execute_tool_block(
        block, session_id="s1", owner="admin", disabled_tools={"manage_git"}, security_context=_no_security_context()
    )
    assert revoked["exit_code"] == 1 and "disabled by user" in revoked["error"]
    handler.assert_not_awaited()


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
