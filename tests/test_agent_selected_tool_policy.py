"""Selected agent bindings remain enforceable after inventories change."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import agent_loadouts, agent_profiles, session_settings, tool_execution
from src.mcp_manager import McpManager


SERVER = "social"
READ = f"mcp__{SERVER}__get-profile"
OTHER_READ = f"mcp__{SERVER}__get-posts"
WRITE = f"mcp__{SERVER}__create-post"


def _no_security_context():
    # Looked up at call time: other tests reload src.tool_execution, which
    # replaces the sentinel execute_tool_block compares by identity.
    import src.tool_execution as tool_execution

    return tool_execution.NO_TOOL_SECURITY_CONTEXT


@pytest.fixture
def manager():
    result = McpManager()
    result._connections[SERVER] = {"status": "connected", "name": "Social MCP"}
    result._tools[SERVER] = [
        {"name": "get-profile", "annotations": {"readOnlyHint": True}},
        {"name": "get-posts", "annotations": {"readOnlyHint": True}},
        {"name": "create-post", "annotations": {"readOnlyHint": False}},
    ]
    result.call_tool = AsyncMock(return_value={"output": "read result", "exit_code": 0})
    return result


def _settings(monkeypatch, settings, manager=None):
    monkeypatch.setattr("core.database.get_session_settings", lambda sid, **kwargs: dict(settings))
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: manager)
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: manager)
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    monkeypatch.setattr("src.tool_security.owner_baseline_disabled_tools", lambda owner: set())
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)


def _execute(tool, arguments=None):
    content = arguments if isinstance(arguments, str) else json.dumps(arguments or {})
    block = SimpleNamespace(tool_type=tool, content=content)
    return asyncio.run(tool_execution.execute_tool_block(
        block, session_id="research-child", disabled_tools=set(), owner=None,security_context=_no_security_context()
    ))


@pytest.mark.parametrize("mode,bindings", [
    ("selected", [READ, "web_search"]), ("none", []), ("all", []),
])
def test_profile_session_patch_preserves_tool_mode_and_bindings(mode, bindings):
    profile = agent_profiles.validate_profiles([{
        "name": "Research specialist", "tool_access": mode, "enabled_tools": bindings,
        "mcp_access": "selected", "allowed_mcp_servers": [SERVER],
    }])[0]
    patch = agent_profiles.session_patch(profile)
    assert patch["tool_access"] == mode
    assert set(patch["enabled_tools"]) == set(bindings)
    persisted = session_settings.validate_patch(patch)
    assert persisted["tool_access"] == mode
    assert set(persisted["enabled_tools"]) == set(bindings)


def test_selected_settings_are_normalized_without_broadening_empty_allowlist():
    patch = session_settings.validate_patch({
        "tool_access": "selected", "enabled_tools": [READ, " web_search ", READ],
        "allowed_mcp_servers": [], "workflow_readonly": True,
    })
    assert patch == {
        "tool_access": "selected", "enabled_tools": sorted([READ, "web_search"]),
        "allowed_mcp_servers": [], "workflow_readonly": True,
    }
    assert session_settings.validate_patch({"tool_access": "none", "enabled_tools": []}) == {
        "tool_access": "none", "enabled_tools": [],
    }


@pytest.mark.parametrize("patch", [
    {"tool_access": "some"}, {"enabled_tools": READ}, {"enabled_tools": [None]},
    {"workflow_readonly": "true"}, {"workflow_readonly": 1},
])
def test_selected_settings_reject_invalid_policy_types(patch):
    with pytest.raises(ValueError):
        session_settings.validate_patch(patch)


def test_caller_policy_unions_current_mcp_inventory_before_selected_clamp(monkeypatch, manager):
    _settings(monkeypatch, {
        "tool_access": "selected", "enabled_tools": [READ, "web_search"],
        "allowed_mcp_servers": [SERVER],
    }, manager)
    monkeypatch.setattr("src.tool_policy.known_tool_names", lambda: {"web_search", "bash"})
    policy = agent_loadouts.caller_policy("parent", None)
    assert {READ, OTHER_READ, WRITE} <= policy["known_tools"]
    assert policy["allowed_tools"] == {READ, "web_search"}
    profile, _ = agent_loadouts.clamp({
        "name": "Content research", "tool_access": "selected", "enabled_tools": [READ],
        "mcp_access": "selected", "allowed_mcp_servers": [SERVER],
    }, policy)
    assert profile["enabled_tools"] == [READ]


def test_caller_policy_explicit_denial_wins_over_selected_binding(monkeypatch, manager):
    _settings(monkeypatch, {
        "tool_access": "selected", "enabled_tools": [READ], "disabled_tools": [READ],
    }, manager)
    policy = agent_loadouts.caller_policy("parent", None)
    assert READ in policy["known_tools"]
    assert READ not in policy["allowed_tools"]


@pytest.mark.parametrize("settings,expected", [({}, ["*"]), ({"allowed_mcp_servers": []}, [])])
def test_caller_policy_distinguishes_default_mcp_access_from_empty(monkeypatch, settings, expected):
    _settings(monkeypatch, settings)
    assert agent_loadouts.caller_policy("parent", None)["allowed_mcp_servers"] == expected


def test_handwritten_newly_connected_mcp_call_cannot_escape_selected_bindings(monkeypatch, manager):
    # The saved snapshot denylist predates this connection and contains no MCP
    # tools. The positive allowlist must remain the authority at call time.
    _settings(monkeypatch, {
        "tool_access": "selected", "enabled_tools": ["web_search"], "disabled_tools": [],
        "allowed_mcp_servers": ["*"],
    }, manager)
    desc, result = _execute(READ)
    assert "BLOCKED" in desc
    assert "selected tool bindings" in result["error"]
    manager.call_tool.assert_not_awaited()


def test_tool_access_none_rejects_stale_selected_list(monkeypatch, manager):
    _settings(monkeypatch, {"tool_access": "none", "enabled_tools": [READ]}, manager)
    _, result = _execute(READ)
    assert result["exit_code"] == 1
    manager.call_tool.assert_not_awaited()


def test_empty_server_allowlist_wins_over_selected_tool_binding(monkeypatch, manager):
    _settings(monkeypatch, {
        "tool_access": "selected", "enabled_tools": [READ], "allowed_mcp_servers": [],
    }, manager)
    _, result = _execute(READ)
    assert "not enabled for this agent" in result["error"]
    manager.call_tool.assert_not_awaited()


@pytest.mark.parametrize("action", ["create", "update", "delete", "save", "install", "run"])
def test_readonly_workflow_rejects_skill_mutations(monkeypatch, action):
    _settings(monkeypatch, {
        "workflow_readonly": True, "tool_access": "selected", "enabled_tools": ["manage_skills"],
    })
    implementation = AsyncMock()
    monkeypatch.setattr("src.tool_implementations.do_manage_skills", implementation)
    _, result = _execute("manage_skills", {"action": action, "name": "research"})
    assert "not modify" in result["error"]
    implementation.assert_not_awaited()


def test_readonly_workflow_can_load_selected_skill(monkeypatch):
    _settings(monkeypatch, {
        "workflow_readonly": True, "tool_access": "selected", "enabled_tools": ["manage_skills"],
        "skill_access": "selected", "skill_names": ["research"],
    })
    implementation = AsyncMock(return_value={"output": "Procedure", "exit_code": 0})
    monkeypatch.setattr("src.tool_implementations.do_manage_skills", implementation)
    _, result = _execute("manage_skills", {"action": "view", "name": "research"})
    assert result["exit_code"] == 0
    implementation.assert_awaited_once()


@pytest.mark.parametrize("name,annotations", [
    ("get-reset", {"readOnlyHint": False}),
    ("get-and-delete", {"readOnlyHint": True, "destructiveHint": True}),
    ("execute-code", None),
])
def test_readonly_workflow_rejects_destructive_mcp_even_if_selected(monkeypatch, manager, name, annotations):
    tool = f"mcp__{SERVER}__{name}"
    manager._tools[SERVER] = [{"name": name, "annotations": annotations}]
    _settings(monkeypatch, {
        "workflow_readonly": True, "tool_access": "selected", "enabled_tools": [tool],
    }, manager)
    _, result = _execute(tool)
    assert "only read-only MCP tools" in result["error"]
    manager.call_tool.assert_not_awaited()


def test_readonly_workflow_allows_authoritatively_readonly_neutral_tool(monkeypatch, manager):
    tool = f"mcp__{SERVER}__timeline"
    manager._tools[SERVER] = [{"name": "timeline", "annotations": {"readOnlyHint": True}}]
    _settings(monkeypatch, {
        "workflow_readonly": True, "tool_access": "selected", "enabled_tools": [tool],
    }, manager)
    _, result = _execute(tool)
    assert result["exit_code"] == 0
    manager.call_tool.assert_awaited_once()


def test_readonly_workflow_rejects_missing_mcp_metadata(monkeypatch, manager):
    _settings(monkeypatch, {
        "workflow_readonly": True, "tool_access": "selected", "enabled_tools": [READ],
    }, manager)
    manager._tools.clear()
    _, result = _execute(READ)
    assert "only read-only MCP tools" in result["error"]
    manager.call_tool.assert_not_awaited()


@pytest.mark.parametrize("binding", ["read_email", "mcp__email__read_email"])
def test_selected_email_alias_does_not_deny_its_equivalent_spelling(monkeypatch, manager, binding):
    aliases = {"read_email", "mcp__email__read_email"}
    manager._connections["email"] = {"status": "connected", "name": "Email"}
    manager._tools["email"] = [{"name": "read_email", "annotations": {"readOnlyHint": True}}]
    _settings(monkeypatch, {"tool_access": "selected", "enabled_tools": [binding]}, manager)
    policy = agent_loadouts.caller_policy("parent", None)
    assert aliases <= policy["allowed_tools"]
    profile, _ = agent_loadouts.clamp({
        "name": "Email reader", "tool_access": "selected", "enabled_tools": [binding],
    }, policy)
    assert profile["enabled_tools"] == [binding]
    assert not aliases.intersection(profile["disabled_tools"])
    patch = agent_profiles.session_patch(profile)
    assert not aliases.intersection(patch["disabled_tools"] or [])
    _settings(monkeypatch, patch, manager)
    for spelling in sorted(aliases):
        _, result = _execute(spelling)
        assert result["exit_code"] == 0
    assert manager.call_tool.await_count == 2


@pytest.mark.parametrize("denied", ["read_email", "mcp__email__read_email"])
def test_explicit_email_denial_wins_over_either_selected_alias(monkeypatch, manager, denied):
    aliases = {"read_email", "mcp__email__read_email"}
    manager._tools["email"] = [{"name": "read_email"}]
    _settings(monkeypatch, {
        "tool_access": "selected", "enabled_tools": sorted(aliases), "disabled_tools": [denied],
    }, manager)
    policy = agent_loadouts.caller_policy("parent", None)
    assert not aliases.intersection(policy["allowed_tools"])
    requested = {"name": "Reader", "tool_access": "selected", "enabled_tools": sorted(aliases)}
    profile, _ = agent_loadouts.clamp(requested, policy)
    assert profile["tool_access"] == "none"
    # A caller allowing both aliases must also honor a denial in the requested loadout.
    policy["allowed_tools"] = aliases
    profile, _ = agent_loadouts.clamp({**requested, "disabled_tools": [denied]}, policy)
    assert profile["enabled_tools"] == []


def test_large_inventory_scope_blocks_first_turn_and_later_mcp_additions(monkeypatch, manager):
    _settings(monkeypatch, {}, manager)
    native = {f"native_tool_{index}" for index in range(250)}
    monkeypatch.setattr("src.tool_policy.known_tool_names", lambda: native)
    policy = agent_loadouts.caller_policy("parent", None)
    profile, _ = agent_loadouts.clamp({
        "name": "Minimal reader", "tool_access": "selected", "enabled_tools": [READ],
        "disabled_tools": [WRITE],
    }, policy)
    assert profile["enabled_tools"] == [READ]
    assert profile["disabled_tools"] == [WRITE]  # explicit denial survives; snapshot isn't truncated
    persisted = agent_profiles.session_patch(agent_profiles.validate_profiles([profile])[0])
    _settings(monkeypatch, persisted, manager)
    _, allowed = _execute(READ)
    assert allowed["exit_code"] == 0
    for name in [OTHER_READ, WRITE, f"mcp__{SERVER}__new-tool"]:
        if name.endswith("new-tool"):
            manager._tools[SERVER].append({"name": "new-tool", "annotations": {"readOnlyHint": True}})
        _, rejected = _execute(name)
        assert rejected["exit_code"] == 1
    manager.call_tool.assert_awaited_once()


def test_caller_policy_cannot_default_to_broad_grants_on_database_failure(monkeypatch):
    def broken_settings(sid, *, strict=False):
        assert strict
        raise RuntimeError("database unavailable")
    monkeypatch.setattr("core.database.get_session_settings", broken_settings)
    with pytest.raises(ValueError, match="no loadout permissions granted"):
        agent_loadouts.caller_policy("parent", None)


def _child_manager():
    parent = SimpleNamespace(endpoint_url="http://localhost/v1", model="test", headers=None)
    child = SimpleNamespace(id="new-child", headers=None)
    return SimpleNamespace(get_session=lambda sid: parent, create_session=lambda **kwargs: child), child


@pytest.mark.parametrize("fails_with_exception", [False, True])
def test_profile_child_does_not_start_without_persisted_policy(monkeypatch, fails_with_exception):
    from src.agent_tools.session_tools import _new_child_session

    def failed_save(sid, patch):
        if fails_with_exception:
            raise RuntimeError("database unavailable")
        return None
    monkeypatch.setattr("core.database.update_session_settings", failed_save)
    manager, _ = _child_manager()
    profile = agent_profiles.validate_profiles([{
        "name": "Reader", "tool_access": "selected", "enabled_tools": [READ],
    }])[0]
    child, error = _new_child_session(manager, "parent", None, "Research", profile)
    assert child is None
    assert "scoped policy" in error and "not started" in error


@pytest.mark.parametrize("profile", [None, {}])
def test_unprofiled_child_keeps_best_effort_parent_metadata(monkeypatch, profile):
    from src.agent_tools.session_tools import _new_child_session

    monkeypatch.setattr("core.database.update_session_settings", lambda sid, patch: None)
    manager, expected = _child_manager()
    child, error = _new_child_session(manager, "parent", None, "Research", profile)
    assert child is expected and error is None


def test_named_profile_for_existing_chat_refuses_without_starting_or_mutating(monkeypatch):
    from src.agent_tools import session_tools

    manager, _ = _child_manager()
    monkeypatch.setattr(session_tools, "get_session_manager", lambda: manager)
    monkeypatch.setattr(agent_profiles, "get_profile", lambda name: {"name": name})
    headless = AsyncMock()
    monkeypatch.setattr("src.headless_agent.run_headless", headless)
    result = asyncio.run(session_tools.send_to_session(json.dumps({
        "session_id": "existing", "message": "Research", "profile": "Reader",
    }), "parent"))
    assert "session_id: 'new'" in result["error"]
    headless.assert_not_awaited()


# ── Read actions of umbrella tools in a read-only workflow (2026-09-22) ─────

def test_readonly_worker_may_list_calendar_events_but_not_create(monkeypatch, manager):
    _settings(monkeypatch, {
        "workflow_readonly": True, "tool_access": "selected", "enabled_tools": ["manage_calendar"],
    }, manager)
    ran = []

    async def fake_calendar(content, owner=None):
        ran.append(json.loads(content)["action"])
        return {"output": "[]", "exit_code": 0}

    monkeypatch.setattr("src.tool_implementations.do_manage_calendar", fake_calendar)
    _, created = _execute("manage_calendar", {"action": "create_event", "title": "x"})
    assert created.get("blocked_reason") == "workflow_readonly"
    assert "may only read" in created["error"]
    _, listed = _execute("manage_calendar", {"action": "list_events"})
    assert listed.get("blocked_reason") != "workflow_readonly"
    assert ran == ["list_events"]


def test_action_is_read_fails_closed():
    from src.tool_capabilities import action_is_read

    assert action_is_read("manage_calendar", '{"action": "list_events"}')
    assert action_is_read("manage_calendar", '{}')  # its default action lists
    assert not action_is_read("manage_calendar", '{"action": "create"}')
    assert not action_is_read("manage_notes", 'not json')
    assert not action_is_read("bash", '{"action": "list"}')
