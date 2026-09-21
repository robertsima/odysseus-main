"""The tools a turn is offered match what the executor will let it run."""

from src.agent_loop import _tools_named_by_user
from src.tool_security import session_policy_disabled_tools


def test_saved_denylist_is_applied():
    denied = session_policy_disabled_tools({"disabled_tools": ["web_search", "create_document"]})
    assert {"web_search", "create_document"} <= denied


def test_selected_access_denies_everything_outside_bindings_but_keeps_discovery():
    mcp = [{"qualified_name": "mcp__srv__lookup", "server_id": "srv"}]
    denied = session_policy_disabled_tools(
        {"tool_access": "selected", "enabled_tools": ["read_file", "mcp__srv__lookup"]}, mcp,
    )
    assert "read_file" not in denied
    assert "mcp__srv__lookup" not in denied
    assert "web_search" in denied
    assert "discover_tools" not in denied


def test_no_access_denies_discovery_too():
    denied = session_policy_disabled_tools({"tool_access": "none"})
    assert {"read_file", "discover_tools"} <= denied


def test_mcp_server_allowlist():
    mcp = [{"qualified_name": "mcp__a__x"}, {"qualified_name": "mcp__b__y"}]
    denied = session_policy_disabled_tools({"allowed_mcp_servers": ["a"]}, mcp)
    assert "mcp__b__y" in denied and "mcp__a__x" not in denied
    assert not session_policy_disabled_tools({"allowed_mcp_servers": ["*"]}, mcp)


def _conv(*turns):
    return [{"role": role, "content": text} for role, text in turns]


def test_tool_named_in_latest_message_is_found():
    known = {"manage_agent_loadout", "read_file"}
    msgs = _conv(("user", "set up the admin agent"), ("assistant", "I lack the tool."),
                 ("user", "Try again with manage_agent_loadout"))
    assert _tools_named_by_user(msgs, known, include_previous=False) == {"manage_agent_loadout"}


def test_low_signal_followup_inherits_previous_user_turn_only():
    known = {"manage_agent_loadout", "manage_skills"}
    msgs = _conv(("user", "Try again with manage_agent_loadout"),
                 ("assistant", "I don't have manage_skills or manage_agent_loadout."),
                 ("user", "u do have it"))
    assert _tools_named_by_user(msgs, known, include_previous=True) == {"manage_agent_loadout"}
    assert _tools_named_by_user(msgs, known, include_previous=False) == set()
