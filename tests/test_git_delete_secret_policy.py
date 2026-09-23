"""Operator policy: agents may use Git and GitHub, but never delete anything
and never read secrets.

Deletes, force pushes and discards are refused outright (not approval-gated);
GitHub MCP delete/remove/archive tools are never exposed and refused at
dispatch; every manage_git result is scrubbed of credentials; raw secret
stores (.git-credentials, .git/config) are off limits to the file tools.
"""

import json
from unittest.mock import AsyncMock

import pytest

from src.agent_tools.git_tools import GitTool, _redact_output
from src.builtin_mcp import GITHUB_MCP_READ_TOOLS, GITHUB_MCP_WRITE_TOOLS
from src.git_tool_contract import POLICY_FORBIDDEN_ACTIONS, policy_refusal
from src.tool_capabilities import ToolEffect, capabilities_for_action, capabilities_for_tool
from src.tool_security import github_mcp_policy_refusal

SHA_A = "a" * 40
SHA_B = "b" * 40
CTX = {"owner": "admin", "session_id": "s1"}


def _admin(monkeypatch):
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda owner: True)


def _set_write_token(monkeypatch, fn):
    # Patch the globals GitTool actually runs with: other tests replace
    # src.agent_tools in sys.modules, so a dotted-path patch can miss.
    monkeypatch.setitem(GitTool._execute.__globals__, "_write_token", fn)


def _call(action, **extra):
    return json.dumps({"action": action, "repository": "/repos/app", **extra})


# ---------------------------------------------------------------- deletes --

FORBIDDEN_CALLS = [
    _call("delete_branch", name="old", expected_head=SHA_A, expected_target=SHA_B),
    _call("delete_remote_branch", remote_branch="old", expected_head=SHA_A, expected_target=SHA_B),
    _call("force_push_with_lease", remote_branch="dev", expected_head=SHA_A, expected_target=SHA_B),
    _call("stash_drop", index=0, expected_target=SHA_B),
    # Also refused when malformed: the policy answer comes before validation,
    # so the agent is not coached into "fixing" the call.
    _call("delete_branch"),
    _call("DELETE_BRANCH", name="old"),
]


@pytest.mark.parametrize("content", FORBIDDEN_CALLS)
def test_forbidden_actions_fail_precheck_so_no_approval_is_ever_requested(content):
    error = GitTool.precheck(content)
    assert error["code"] == "forbidden_by_policy"
    assert "not permitted by policy" in error["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("content", FORBIDDEN_CALLS)
async def test_forbidden_actions_never_reach_the_service(monkeypatch, content):
    _admin(monkeypatch)
    remote = AsyncMock(return_value={"ok": True})
    history = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("src.agent_worktree.repository_remote.execute_remote", remote)
    monkeypatch.setattr("src.agent_worktree.repository_history.execute_history", history)
    _set_write_token(monkeypatch, lambda ctx: "ghp_" + "x" * 36)
    result = await GitTool().execute(content, CTX)
    assert result["exit_code"] == 1 and result["code"] == "forbidden_by_policy"
    remote.assert_not_awaited()
    history.assert_not_awaited()


@pytest.mark.parametrize(
    "action",
    [
        "force_push", "push_force", "push_delete", "push_mirror", "push_prune",
        "clean", "git_clean", "reset_hard", "checkout_discard", "restore",
        "stash_clear", "reflog_expire", "gc", "gc_prune", "worktree_remove",
        "worktree_prune", "filter_branch", "filter-repo", "update_ref_delete",
        "tag_delete", "rm", "remove_branch", "purge",
    ],
)
def test_invented_destructive_actions_get_the_policy_answer(action):
    error = GitTool.precheck(_call(action))
    assert error["code"] == "forbidden_by_policy", action


def test_unknown_harmless_action_is_still_merely_unsupported():
    assert GitTool.precheck(_call("cherry_pick"))["code"] == "unsupported_action"


def test_policy_list_matches_the_advertised_schema():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    schema = next(s for s in FUNCTION_TOOL_SCHEMAS if s["function"]["name"] == "manage_git")
    advertised = set(schema["function"]["parameters"]["properties"]["action"]["enum"])
    assert not advertised & set(POLICY_FORBIDDEN_ACTIONS)
    assert all(policy_refusal(a) is None for a in advertised)


def test_forbidden_actions_stay_classified_destructive():
    for action in POLICY_FORBIDDEN_ACTIONS:
        caps = capabilities_for_action("manage_git", _call(action))
        assert ToolEffect.DESTRUCTIVE in caps.effects, action
    remove = capabilities_for_action("manage_agent_worktree", '{"action":"remove","name":"x"}')
    assert ToolEffect.DESTRUCTIVE in remove.effects


# ------------------------------------------------------ normal flows work --

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,expected_args",
    [
        (_call("status"), {}),
        (_call("commit", message="Fix it"), {"message": "Fix it"}),
        (_call("branch", name="feature/x"), {"name": "feature/x"}),
        (_call("tag", name="v1.0"), {"name": "v1.0"}),
    ],
)
async def test_local_flows_still_dispatch(monkeypatch, content, expected_args):
    _admin(monkeypatch)
    local = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("src.agent_worktree.repository_local.execute_local", local)
    result = await GitTool().execute(content, CTX)
    assert result == {"exit_code": 0, "result": {"ok": True}}
    action = json.loads(content)["action"]
    local.assert_awaited_once_with(action, "/repos/app", **expected_args)


@pytest.mark.asyncio
async def test_fast_forward_push_still_dispatches(monkeypatch):
    _admin(monkeypatch)
    remote = AsyncMock(return_value={"ok": True, "updated": True})
    monkeypatch.setattr("src.agent_worktree.repository_remote.execute_remote", remote)
    monkeypatch.setattr("src.agent_worktree.repository_remote.publish_refusal", lambda p, a: None)
    _set_write_token(monkeypatch, lambda ctx: "ghp_" + "t" * 36)
    content = _call("push", remote_branch="feature/x", expected_head=SHA_A)
    assert GitTool.precheck(content) is None
    result = await GitTool().execute(content, CTX)
    assert result["exit_code"] == 0
    remote.assert_awaited_once()
    assert remote.await_args.kwargs["remote_branch"] == "feature/x"


# ------------------------------------------------------- output redaction --

TOKEN = "ghp_" + "Z" * 36
PAT = "github_pat_" + "Q" * 40


def test_redaction_scrubs_userinfo_urls_and_token_shapes():
    out = _redact_output(
        {
            "url": f"https://x-access-token:{TOKEN}@github.com/o/r.git",
            "log": [f"leaked {PAT} and gho_{'a' * 30}", {"m": "Authorization: Bearer abc.def-123"}],
            "n": 3,
        }
    )
    text = json.dumps(out)
    assert TOKEN not in text and PAT not in text and "gho_" not in text and "abc.def-123" not in text
    assert out["url"] == "https://***@github.com/o/r.git"
    assert out["n"] == 3


def test_redaction_leaves_ordinary_diff_content_alone():
    diff = '+ url = "https://example.com/api?page=2"\n+ password = os.environ["PW"]\n'
    assert _redact_output(diff) == diff


@pytest.mark.asyncio
async def test_every_manage_git_result_and_error_is_redacted(monkeypatch):
    _admin(monkeypatch)
    local = AsyncMock(return_value={"log": [{"message": f"oops {TOKEN}"}]})
    monkeypatch.setattr("src.agent_worktree.repository_local.execute_local", local)
    result = await GitTool().execute(_call("log"), CTX)
    assert TOKEN not in json.dumps(result)

    from src.agent_worktree import repository_sync as sync

    failing = AsyncMock(side_effect=sync.RepositorySyncError(
        "remote_failed", f"fetch failed for https://u:{TOKEN}@github.com/o/r"))
    monkeypatch.setattr("src.agent_worktree.repository_local.execute_local", failing)
    result = await GitTool().execute(_call("status"), CTX)
    assert result["exit_code"] == 1 and TOKEN not in result["error"]


def test_no_raw_git_or_credential_config_surface():
    """manage_git takes typed actions only: no argv, no config/credential reads."""
    for action in ("config", "credential", "credential_fill", "remote_v", "raw", "git"):
        error = GitTool.precheck(_call(action, args=["config", "-l"]))
        assert error["code"] in {"unsupported_action", "forbidden_by_policy"}, action


# ------------------------------------------------------------ GitHub MCP --

def test_builtin_github_tool_lists_contain_no_destructive_tool():
    for name in GITHUB_MCP_READ_TOOLS + GITHUB_MCP_WRITE_TOOLS:
        assert github_mcp_policy_refusal(f"mcp__github_write__{name}") is None, name
        assert not any(w in name for w in ("delete", "remove", "archive")), name


@pytest.mark.parametrize(
    "name",
    [
        "mcp__github__delete_file",
        "mcp__github__delete_repository",
        "mcp__github_write__delete_branch",
        "mcp__GitHub__remove_sub_issue",
        "mcp__github_enterprise__archive_repository",
    ],
)
def test_github_destructive_tools_are_refused_and_destructive(name):
    assert "not permitted by policy" in github_mcp_policy_refusal(name, {}).lower()
    assert ToolEffect.DESTRUCTIVE in capabilities_for_tool(name).effects


def test_github_destructive_methods_are_refused():
    assert github_mcp_policy_refusal(
        "mcp__github_write__pull_request_review_write", {"method": "delete_pending"})
    assert github_mcp_policy_refusal(
        "mcp__github__sub_issue_write", json.dumps({"method": "remove"}))
    assert github_mcp_policy_refusal(
        "mcp__github_write__pull_request_review_write", {"method": "create"}) is None
    # Other servers are out of scope for this policy.
    assert github_mcp_policy_refusal("mcp__todoist__delete_task", {}) is None


@pytest.mark.asyncio
async def test_mcp_manager_refuses_and_hides_github_deletes():
    from src.mcp_manager import McpManager, _policy_visible_tools

    manager = McpManager()
    result = await manager.call_tool("mcp__github__delete_file", {"path": "x"})
    assert result["exit_code"] == 1 and result["code"] == "forbidden_by_policy"

    tools = [{"name": "create_pull_request"}, {"name": "delete_file"}, {"name": "list_branches"}]
    assert [t["name"] for t in _policy_visible_tools("github", tools)] == [
        "create_pull_request", "list_branches"]
    assert len(_policy_visible_tools("todoist", tools)) == 3


# --------------------------------------------------- file-tool secret stores --

@pytest.mark.parametrize(
    "path,sensitive",
    [
        ("/data/repos/app/.git-credentials", True),
        ("/data/repos/app/.git/config", True),
        ("/data/repos/app/.GIT/CONFIG", True),
        ("/data/repos/app/.env", True),
        ("/data/repos/app/src/config", False),
        ("/data/repos/app/.git/HEAD", False),
    ],
)
def test_file_tools_deny_repository_secret_stores(path, sensitive):
    from src.tool_execution import _is_sensitive_path

    assert _is_sensitive_path(path) is sensitive
