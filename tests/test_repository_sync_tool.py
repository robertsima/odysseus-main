"""Agent wrapper boundaries for the narrow repository synchronization service."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agent_tools.worktree_tools import AgentWorktreeTool, _repository_read_token
from src.agent_worktree import repository_sync as repo_sync
from src import tool_execution
from src.tool_types import ToolBlock

_REPORT_BACKLOG = pytest.mark.skip(
    reason="Re-port backlog: uses fork-only internals replaced by upstream's agent core (website/upstream-sync-2026-09-18.md)"
)


def _no_security_context():
    # Looked up at call time: other tests reload src.tool_execution, which
    # replaces the sentinel execute_tool_block compares by identity.
    import src.tool_execution as tool_execution

    return tool_execution.NO_TOOL_SECURITY_CONTEXT


@_REPORT_BACKLOG
@pytest.mark.parametrize("mode", ["ask_all", "ask_risky"])
def test_repository_approval_distinguishes_inspection_from_pull(mode):
    from src.tool_approvals import approval_reason

    for action in ("repo_list", "repo_status"):
        assert (
            approval_reason(
                "manage_agent_worktree", json.dumps({"action": action}), mode
            )
            is None
        )
    assert "fast-forwards" in approval_reason(
        "manage_agent_worktree",
        '{"action":"repo_pull"}',
        mode,
    )
    assert (
        approval_reason("manage_agent_worktree", '{"action":"repo_pull"}', "auto")
        is None
    )


def _admin(monkeypatch, allowed=True):
    monkeypatch.setattr(
        "src.tool_security.owner_is_admin_or_single_user",
        lambda owner: allowed,
    )
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: allowed)
    monkeypatch.setattr(
        "src.tool_security.owner_baseline_disabled_tools", lambda owner: set()
    )
    monkeypatch.setattr("src.settings.load_disabled_tools_strict", lambda: [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,service_name,expected_key",
    [
        ("repo_list", "list_repositories", "repositories"),
        ("repo_status", "repository_status", "result"),
        ("repo_pull", "pull_repository", "result"),
    ],
)
async def test_repository_actions_dispatch_only_to_narrow_service(
    monkeypatch, action, service_name, expected_key
):
    _admin(monkeypatch)
    expected = [{"repository": "/repos/app"}] if action == "repo_list" else {"ok": True}
    implementation = AsyncMock(return_value=expected)
    monkeypatch.setattr(repo_sync, service_name, implementation)
    args = {"action": action}
    if action != "repo_list":
        args["repository"] = "/repos/app"

    result = await AgentWorktreeTool().execute(json.dumps(args), {"owner": "admin"})

    assert result["exit_code"] == 0
    assert result[expected_key] == expected
    if action == "repo_list":
        implementation.assert_awaited_once_with()
    else:
        implementation.assert_awaited_once()


@pytest.mark.asyncio
async def test_repository_actions_are_admin_only_before_service_dispatch(monkeypatch):
    _admin(monkeypatch, allowed=False)
    implementation = AsyncMock(side_effect=AssertionError("service must not run"))
    monkeypatch.setattr(repo_sync, "list_repositories", implementation)

    result = await AgentWorktreeTool().execute(
        '{"action":"repo_list"}', {"owner": "public-user"}
    )

    assert result["exit_code"] == 1
    assert "admin" in result["error"].lower()
    implementation.assert_not_awaited()


@pytest.mark.parametrize(
    "settings,host,expected",
    [
        ({}, "", "ghp_test_secret"),
        ({"allowed_mcp_servers": None}, "github.com", "ghp_test_secret"),
        ({"allowed_mcp_servers": ["*"]}, "https://github.com", "ghp_test_secret"),
        ({"allowed_mcp_servers": ["github_read"]}, "github.com", "ghp_test_secret"),
        ({"allowed_mcp_servers": []}, "github.com", None),
        ({"allowed_mcp_servers": ["calendar"]}, "github.com", None),
        # Enterprise: the same token as MCP; the transport binds it to that host.
        ({"allowed_mcp_servers": ["github_read"]}, "github.enterprise", "ghp_test_secret"),
    ],
)
def test_repository_token_requires_fresh_session_permission_and_github_host(
    monkeypatch, settings, host, expected
):
    calls = []

    def fresh_settings(session_id, *, strict=False):
        calls.append((session_id, strict))
        return settings

    monkeypatch.setattr("core.database.get_session_settings", fresh_settings)
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_test_secret")
    monkeypatch.setenv("GITHUB_HOST", host)
    assert _repository_read_token({"session_id": "chat-1"}) == expected
    assert calls == [("chat-1", True)]


def test_repository_read_token_rejects_cross_service_secret(monkeypatch):
    monkeypatch.setattr("core.database.get_session_settings", lambda *_a, **_k: {})
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "ody_not_a_github_token")
    monkeypatch.delenv("GITHUB_HOST", raising=False)
    assert _repository_read_token({"session_id": "chat-1"}) is None


def test_repository_token_is_never_read_without_session(monkeypatch):
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "secret")
    monkeypatch.setattr(
        "core.database.get_session_settings",
        lambda *args, **kwargs: pytest.fail("session settings should not be read"),
    )
    assert _repository_read_token({}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("override", ["branch", "remote", "command"])
async def test_repository_actions_reject_execution_overrides(monkeypatch, override):
    _admin(monkeypatch)
    implementation = AsyncMock(side_effect=AssertionError("service must not run"))
    monkeypatch.setattr(repo_sync, "pull_repository", implementation)
    args = {"action": "repo_pull", "repository": "/repos/app", override: "attacker"}

    result = await AgentWorktreeTool().execute(json.dumps(args), {"owner": "admin"})

    assert result["exit_code"] == 1
    implementation.assert_not_awaited()


@pytest.mark.asyncio
async def test_repository_service_errors_are_safe_and_unexpected_errors_are_redacted(
    monkeypatch,
):
    _admin(monkeypatch)
    tool = AgentWorktreeTool()
    monkeypatch.setattr(
        repo_sync,
        "repository_status",
        AsyncMock(
            side_effect=repo_sync.RepositorySyncError(
                "dirty_tree", "Working tree is dirty."
            )
        ),
    )
    safe = await tool.execute(
        '{"action":"repo_status","repository":"/repos/app"}', {"owner": "admin"}
    )
    assert safe["exit_code"] == 1
    assert safe["code"] == "dirty_tree"
    assert safe["error"] == "Working tree is dirty."

    monkeypatch.setattr(
        repo_sync,
        "repository_status",
        AsyncMock(side_effect=RuntimeError("token=super-secret internal traceback")),
    )
    unexpected = await tool.execute(
        '{"action":"repo_status","repository":"/repos/app"}', {"owner": "admin"}
    )
    assert unexpected["exit_code"] == 1
    assert unexpected["code"] == "repository_sync_failed"
    assert unexpected["error"].startswith("Repository operation failed;")
    assert "super-secret" not in unexpected["error"]


@pytest.mark.asyncio
async def test_repository_sync_does_not_require_private_vault_grant(monkeypatch):
    _admin(monkeypatch)
    implementation = AsyncMock(return_value=[])
    monkeypatch.setattr(repo_sync, "list_repositories", implementation)
    result = await AgentWorktreeTool().execute(
        '{"action":"repo_list"}', {"owner": "admin", "allow_private": False}
    )
    assert result == {"exit_code": 0, "repositories": []}


@pytest.mark.asyncio
async def test_dispatcher_still_enforces_profile_plan_and_active_policy_revocation(
    monkeypatch,
):
    _admin(monkeypatch)
    handler = AsyncMock(return_value={"exit_code": 0, "repositories": []})
    monkeypatch.setitem(
        __import__("src.agent_tools", fromlist=["TOOL_HANDLERS"]).TOOL_HANDLERS,
        "manage_agent_worktree",
        handler,
    )
    monkeypatch.setattr(
        "core.database.get_session_settings",
        lambda session_id, strict=False: {
            "tool_access": "selected",
            "enabled_tools": ["web_search"],
        },
    )
    block = ToolBlock("manage_agent_worktree", '{"action":"repo_list"}')
    _desc, profile_blocked = await tool_execution.execute_tool_block(
        block, session_id="chat-1", owner="admin", security_context=_no_security_context()
    )
    assert profile_blocked["exit_code"] == 1
    assert "selected tool bindings" in profile_blocked["error"]
    handler.assert_not_awaited()

    monkeypatch.setattr("core.database.get_session_settings", lambda *a, **k: {})
    _desc, plan_blocked = await tool_execution.execute_tool_block(
        block,
        session_id="chat-1",
        owner="admin",
        disabled_tools={"manage_agent_worktree"},security_context=_no_security_context()
    )
    assert plan_blocked["exit_code"] == 1
    assert "disabled by user" in plan_blocked["error"]
    handler.assert_not_awaited()

    policy = SimpleNamespace(blocks=lambda name: name == "manage_agent_worktree")
    _desc, policy_blocked = await tool_execution.execute_tool_block(
        block, session_id="chat-1", owner="admin", tool_policy=policy, security_context=_no_security_context()
    )
    assert policy_blocked["exit_code"] == 1
    assert "guide-only policy" in policy_blocked["error"]
    handler.assert_not_awaited()
