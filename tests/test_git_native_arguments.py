"""Regression coverage for provider-expanded native Git arguments.

Some function-calling providers send every advertised property, filling fields
which do not apply to the selected action with JSON-neutral values.  These tests
keep that transport quirk separate from authority: only declared, irrelevant
neutral fields and documented optional defaults may be discarded, while required or unknown
fields continue through normal validation and approval binding.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from src import tool_approvals, tool_execution
from src.agent_loop import _resolve_tool_blocks
from src.chatgpt_subscription import build_responses_tools
from src.git_tool_contract import (
    normalize_git_arguments,
    normalize_worktree_repo_arguments,
)
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, compact_function_tool_schemas

_REPORT_BACKLOG = pytest.mark.skip(
    reason="Re-port backlog: uses fork-only internals replaced by upstream's agent core (website/upstream-sync-2026-09-18.md)"
)

_NEUTRAL_GIT_PAYLOAD = {
    "repository": "",
    "paths": [],
    "name": "",
    "ref": "",
    "message": "",
    "author_name": "",
    "author_email": "",
    "limit": 0,
    "staged": False,
    "remote_branch": "",
    "remote": "",
    "source": "",
    "branch": "",
    "depth": 0,
    "initial_branch": "",
    "index": 0,
    "expected_head": "",
    "expected_target": "",
}


def _no_security_context():
    # Looked up at call time: other tests reload src.tool_execution, which
    # replaces the sentinel execute_tool_block compares by identity.
    import src.tool_execution as tool_execution

    return tool_execution.NO_TOOL_SECURITY_CONTEXT


def _schema(name: str) -> dict:
    return next(
        schema
        for schema in FUNCTION_TOOL_SCHEMAS
        if schema.get("function", {}).get("name") == name
    )


def _admin(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.tool_security.owner_is_admin_or_single_user", lambda _owner: True
    )
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda _owner: True)
    monkeypatch.setattr(
        "src.tool_security.owner_baseline_disabled_tools", lambda _owner: set()
    )
    monkeypatch.setattr("src.settings.load_disabled_tools_strict", list)


@pytest.fixture(autouse=True)
def _clean_approval_state():
    yield


def test_normalizer_prunes_only_declared_irrelevant_neutral_fields():
    expanded = {"action": "repositories", **_NEUTRAL_GIT_PAYLOAD}

    normalized = normalize_git_arguments(expanded)

    assert normalized == {"action": "repositories"}
    assert expanded == {"action": "repositories", **_NEUTRAL_GIT_PAYLOAD}


@pytest.mark.parametrize(
    ("args", "retained"),
    [
        ({"action": "commit", "repository": "/repo", "message": ""}, "message"),
        ({"action": "stage", "repository": "/repo", "paths": []}, "paths"),
        ({"action": "merge", "repository": "/repo", "ref": ""}, "ref"),
        (
            {"action": "push", "repository": "/repo", "expected_head": ""},
            "expected_head",
        ),
        (
            {"action": "set_upstream", "repository": "/repo", "remote_branch": ""},
            "remote_branch",
        ),
        ({"action": "clone", "repository": "/repo", "source": ""}, "source"),
        (
            {"action": "reset", "repository": "/repo", "expected_target": ""},
            "expected_target",
        ),
    ],
)
def test_normalizer_retains_required_values_for_validation(args, retained):
    assert retained in normalize_git_arguments(args)


@pytest.mark.parametrize(
    "action,defaults",
    [
        ("log", {"limit": 0, "ref": ""}),
        ("commit", {"author_name": "", "author_email": ""}),
        ("branch", {"ref": ""}),
        ("tag", {"ref": None}),
        ("diff", {"staged": False}),
        ("push", {"remote_branch": ""}),
        ("set_upstream", {"remote": ""}),
        ("fetch_branch", {"remote": ""}),
        ("clone", {"branch": "", "depth": 0}),
        ("init", {"initial_branch": ""}),
        ("stash_create", {"message": ""}),
        ("stash_apply", {"index": 0}),
    ],
)
def test_only_explicit_defaultable_fields_treat_neutral_values_as_omission(
    action, defaults
):
    base = {"action": action, "repository": "/repo"}
    assert normalize_git_arguments({**base, **defaults}) == base


def test_normalizer_does_not_turn_invalid_or_unknown_input_into_authority():
    args = {
        "action": "status",
        "repository": "/repo",
        "name": "main",
        "remote": "origin",
        "approved": False,
        "command": "",
    }
    assert normalize_git_arguments(args) == args


def test_legacy_repository_normalizer_is_equally_narrow_and_nonmutating():
    expanded = {
        "action": "repo_list",
        "repository": "",
        "name": "",
        "branch": "",
        "message": "",
        "title": "",
        "body": "",
        "request_id": "",
        "approval_code": "",
    }
    assert normalize_worktree_repo_arguments(expanded) == {"action": "repo_list"}
    assert expanded["repository"] == ""
    invalid = {**expanded, "command": "", "approved": False}
    assert normalize_worktree_repo_arguments(invalid)["command"] == ""
    assert normalize_worktree_repo_arguments(invalid)["approved"] is False


@pytest.mark.asyncio
async def test_expanded_legacy_repo_list_normalizes_before_strict_dispatch(
    monkeypatch,
):
    _admin(monkeypatch)
    implementation = AsyncMock(return_value=[{"path": "/repos/app"}])
    monkeypatch.setattr(
        "src.agent_worktree.repository_sync.list_repositories", implementation
    )
    args = {
        "action": "repo_list",
        "repository": "",
        "name": "",
        "branch": "",
        "message": "",
        "title": "",
        "body": "",
        "request_id": "",
        "approval_code": "",
    }
    blocks, used_native, _calls = _resolve_tool_blocks(
        "",
        [
            {
                "id": "repo-1",
                "name": "manage_agent_worktree",
                "arguments": json.dumps(args),
            }
        ],
        1,
        is_api_model=True,
    )
    assert used_native is True
    assert json.loads(blocks[0].content) == args

    _description, result = await tool_execution.execute_tool_block(
        blocks[0], owner="admin", security_context=_no_security_context()
    )

    assert result["exit_code"] == 0
    implementation.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["repositories", "status"])
async def test_expanded_native_call_reaches_real_dispatcher_with_sparse_service_args(
    monkeypatch, action
):
    _admin(monkeypatch)
    list_repositories = AsyncMock(return_value=[{"path": "/repos/app"}])
    execute_local = AsyncMock(return_value={"clean": True})
    monkeypatch.setattr(
        "src.agent_worktree.repository_sync.list_repositories", list_repositories
    )
    monkeypatch.setattr(
        "src.agent_worktree.repository_local.execute_local", execute_local
    )
    args = {"action": action, **_NEUTRAL_GIT_PAYLOAD}
    if action == "status":
        args["repository"] = "/repos/app"
    original = json.dumps(args)
    blocks, used_native, _calls = _resolve_tool_blocks(
        "",
        [{"id": "git-1", "name": "manage_git", "arguments": original}],
        1,
        is_api_model=True,
    )
    # Conversion is lossless; normalization belongs at the execution boundary.
    assert used_native is True
    assert json.loads(blocks[0].content) == args

    _description, result = await tool_execution.execute_tool_block(
        blocks[0], owner="admin", security_context=_no_security_context()
    )

    assert result["exit_code"] == 0
    if action == "repositories":
        list_repositories.assert_awaited_once_with()
        execute_local.assert_not_awaited()
    else:
        execute_local.assert_awaited_once_with("status", "/repos/app")
        list_repositories.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_and_meaningful_disallowed_fields_still_fail_closed(monkeypatch):
    _admin(monkeypatch)
    from src.agent_tools.git_tools import GitTool

    for extra in (
        {"approved": False},
        {"command": ""},
        {"name": "main"},
        {"remote": "origin"},
    ):
        result = await GitTool().execute(
            json.dumps({"action": "status", "repository": "/repos/app", **extra}),
            {"owner": "admin"},
        )
        assert result["code"] == "invalid_arguments", extra


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
@pytest.mark.parametrize("action", ["pull", "pull_with_restore"])
async def test_expanded_pull_uses_only_permission_checked_integration_token(
    monkeypatch, allowed, action
):
    _admin(monkeypatch)
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_test_only_github_token")
    monkeypatch.delenv("GITHUB_HOST", raising=False)
    monkeypatch.setattr(
        "core.database.get_session_settings",
        lambda *a, **kw: {"allowed_mcp_servers": ["github_read"] if allowed else []},
    )
    implementation = AsyncMock(return_value={"updated": True})
    monkeypatch.setattr(
        "src.agent_worktree.repository_remote.execute_remote", implementation
    )
    args = {**_NEUTRAL_GIT_PAYLOAD, "action": action, "repository": "/repos/app"}
    blocks, used_native, _calls = _resolve_tool_blocks(
        "",
        [{"id": "git-pull", "name": "manage_git", "arguments": json.dumps(args)}],
        1,
        is_api_model=True,
    )
    assert used_native
    _description, result = await tool_execution.execute_tool_block(
        blocks[0], owner="admin", session_id="git-session", security_context=_no_security_context()
    )
    assert result["exit_code"] == 0
    implementation.assert_awaited_once_with(
        action, "/repos/app", token="ghp_test_only_github_token" if allowed else None
    )


@_REPORT_BACKLOG
def test_one_use_approval_equates_sparse_and_provider_expanded_calls_only():
    sparse = {
        "action": "push",
        "repository": "/repos/app",
        "expected_head": "a" * 40,
    }
    expanded = {**_NEUTRAL_GIT_PAYLOAD, **sparse}
    pending = tool_approvals.request(
        "session", "manage_git", json.dumps(sparse), "pushes"
    )
    tool_approvals.decide("session", pending["id"], "once")
    assert tool_approvals.has_once_grant("session", "manage_git", json.dumps(expanded))
    assert tool_approvals.consume_once_grant(
        "session", "manage_git", json.dumps(expanded)
    )
    assert not tool_approvals.has_once_grant(
        "session", "manage_git", json.dumps(sparse)
    )


@_REPORT_BACKLOG
@pytest.mark.parametrize("approve_expanded", [True, False])
def test_legacy_repo_pull_approval_equates_neutral_fillers_but_not_targets(
    approve_expanded,
):
    sparse = {"action": "repo_pull", "repository": "/repos/app"}
    expanded = {
        **sparse,
        "name": "",
        "branch": "",
        "message": "",
        "title": "",
        "body": "",
        "request_id": "",
        "approval_code": "",
    }
    approved, retried = (expanded, sparse) if approve_expanded else (sparse, expanded)
    pending = tool_approvals.request(
        "session", "manage_agent_worktree", json.dumps(approved), "pulls"
    )
    tool_approvals.decide("session", pending["id"], "once")
    for change in (
        {"repository": "/repos/other"},
        {"repository": "/repos/app "},
        {"branch": "main"},
    ):
        assert not tool_approvals.has_once_grant(
            "session", "manage_agent_worktree", json.dumps({**retried, **change})
        )
    assert tool_approvals.consume_grant(
        "session", "manage_agent_worktree", json.dumps(retried)
    )
    assert not tool_approvals.consume_grant(
        "session", "manage_agent_worktree", json.dumps(approved)
    )


@_REPORT_BACKLOG
@pytest.mark.parametrize(
    "change",
    [
        {"repository": "/repos/other"},
        {"expected_head": "b" * 40},
        {"remote_branch": "release"},
    ],
)
def test_one_use_approval_never_equates_meaningful_target_changes(change):
    approved = {
        "action": "push",
        "repository": "/repos/app",
        "expected_head": "a" * 40,
    }
    pending = tool_approvals.request(
        "session", "manage_git", json.dumps(approved), "pushes"
    )
    tool_approvals.decide("session", pending["id"], "once")
    assert not tool_approvals.has_once_grant(
        "session", "manage_git", json.dumps({**approved, **change})
    )
    assert tool_approvals.has_once_grant("session", "manage_git", json.dumps(approved))


@pytest.mark.parametrize("name", ["manage_git", "manage_agent_worktree"])
def test_compact_provider_contract_keeps_action_only_required_and_guidance(name):
    compact = compact_function_tool_schemas([_schema(name)])[0]
    native = build_responses_tools([compact])[0]
    parameters = native["parameters"]

    assert native["strict"] is False
    assert parameters["required"] == ["action"]
    assert "default" not in parameters["properties"]["action"]
    assert parameters["properties"]["repository"].get("description")
    first_sentence = native["description"].split(".", 1)[0].lower()
    assert "only" in first_sentence
    assert "action" in first_sentence


def test_compact_git_payload_preserves_parameter_descriptions():
    compact = compact_function_tool_schemas([_schema("manage_git")])[0]
    properties = compact["function"]["parameters"]["properties"]
    assert properties["repository"]["description"]
    assert properties["paths"]["description"]
    assert properties["remote_branch"]["description"]
    assert "pull_with_restore" in properties["action"]["enum"]
