"""MCP wildcard grants are grants, not unknown tool names.

2026-09-27 diagnostics: the "Odysseus Admin" loadout read DEGRADED with a single
failed check, ``tool mcp__* — unknown: no native tool or connected MCP tool has
this name``. ``mcp__*`` (every connected server) and ``mcp__<server>__*`` (one
whole server) are the documented way to grant MCP in ``enabled_tools``, so
readiness now reports what each one reaches instead.
"""

import pytest

from src import agent_loadouts
from src.profile_readiness import profile_readiness, render


MCP_ROWS = {
    "mcp__todoist__add_task": ("todoist", "connected"),
    "mcp__todoist__list_tasks": ("todoist", "connected"),
    "mcp__penpot__create_frame": ("penpot", "connected"),
    "mcp__lotus__mood_summary": ("lotus", "disconnected"),
}


def _policy():
    known = {"read_file", "grep", "web_search"}
    return {
        "allowed_tools": set(known) | set(MCP_ROWS), "known_tools": set(known),
        "tool_access": "all", "enabled_tools": [], "denied_tools": set(),
        "allowed_mcp_servers": ["*"], "worker_depth": 0,
    }


def _profile(*enabled, **extra):
    return {"name": "Odysseus Admin", "tool_access": "selected", "enabled_tools": list(enabled),
            "mcp_access": "all", "private_vault_access": False, **extra}


@pytest.fixture
def live(monkeypatch):
    state = {"rows": dict(MCP_ROWS), "deferred": {"mcp__penpot__create_frame"}}
    monkeypatch.setattr(agent_loadouts, "_mcp_state", lambda: (state["rows"], state["deferred"]))
    monkeypatch.setattr("src.tool_security.owner_baseline_disabled_tools", lambda owner: set())
    monkeypatch.setattr("src.retrieval_health.cached_problems", lambda: [])
    return state


def _failed(readiness):
    return {row["check"]: row for row in readiness["checks"] if not row["ok"]}


def test_all_servers_wildcard_is_ready_and_reported_as_a_group(live):
    readiness = profile_readiness(_profile("read_file", "mcp__*"), _policy(), "u")

    assert readiness["status"] == "READY", render(readiness)
    assert _failed(readiness) == {}
    rows = {row["check"]: row for row in readiness["checks"]}
    detail = rows["tool mcp__*"]["detail"]
    assert detail.startswith("mcp__* → 2 connected servers, 3 tools")
    assert "1 attached on demand" in detail
    # The disconnected server is named, but does not fail an "everything
    # connected" grant.
    assert "lotus is disconnected" in detail
    assert "1 bound tool(s) callable" in rows["tools"]["detail"]
    assert "plus 3 MCP tool(s) via mcp__*" in rows["tools"]["detail"]

    group = readiness["capabilities"]["mcp_groups"][0]
    assert group["entry"] == "mcp__*"
    assert group["servers"] == ["penpot", "todoist"]
    assert group["tool_count"] == 3
    assert "mcp__*" in readiness["capabilities"]["known"]


def test_all_servers_wildcard_with_nothing_connected_is_not_a_failure(live):
    live["rows"] = {}
    live["deferred"] = set()
    readiness = profile_readiness(_profile("read_file", "mcp__*"), _policy(), "u")

    assert readiness["status"] == "READY", render(readiness)
    detail = {row["check"]: row for row in readiness["checks"]}["tool mcp__*"]["detail"]
    assert "0 tools" in detail and "no MCP server is connected" in detail


def test_server_wildcards_expand_per_server(live):
    readiness = profile_readiness(
        _profile("mcp__todoist__*", "mcp__lotus__*", "mcp__ghost__*"), _policy(), "u")

    assert readiness["status"] == "DEGRADED"
    rows = {row["check"]: row for row in readiness["checks"]}
    assert rows["tool mcp__todoist__*"]["ok"]
    assert rows["tool mcp__todoist__*"]["detail"].startswith("mcp__todoist__* → 1 connected server, 2 tools")
    failed = _failed(readiness)
    assert set(failed) == {"tool mcp__lotus__*", "tool mcp__ghost__*"}
    assert "mcp_server: MCP server lotus is disconnected" == failed["tool mcp__lotus__*"]["detail"]
    assert "ghost offers no tools" in failed["tool mcp__ghost__*"]["detail"]
    assert all("unknown" not in row["detail"] for row in failed.values())


def test_a_required_server_wildcard_that_reaches_nothing_blocks(live):
    readiness = profile_readiness(_profile("mcp__lotus__*"), _policy(), "u",
                                  required_tools=["mcp__lotus__*"])
    assert readiness["status"] == "BLOCKED"
    assert readiness["capabilities"]["mission_critical_missing"] == ["mcp__lotus__*"]


def test_all_access_profile_listing_the_wildcard_is_not_unknown(live):
    """The production shape: before this, `mcp__*` fell through to
    `tool not in known` and read "unknown"."""
    readiness = profile_readiness(
        _profile("read_file", "mcp__*", "no_such_tool", tool_access="all"), _policy(), "u")
    failed = _failed(readiness)
    # A genuinely misspelled name is still unknown; the wildcard is not.
    assert set(failed) == {"tool no_such_tool"}
    assert failed["tool no_such_tool"]["detail"].startswith("unknown:")
    rows = {row["check"]: row for row in readiness["checks"]}
    assert rows["tool mcp__*"]["ok"]
    assert rows["tool mcp__*"]["detail"].startswith("mcp__* → 2 connected servers, 3 tools")


def test_a_wildcard_the_caller_cannot_grant_is_parent_policy(live):
    caller = {**_policy(), "allowed_mcp_servers": ["todoist"]}
    readiness = profile_readiness(
        _profile("read_file", tool_access="all"), caller, "u", requested_tools=["mcp__*"])
    detail = _failed(readiness)["tool mcp__*"]["detail"]
    assert detail.startswith("parent_policy:")


def test_wildcard_the_profile_disables_is_still_a_conflict(live):
    readiness = profile_readiness(
        _profile("read_file", "mcp__*", disabled_tools=["mcp__*"]), _policy(), "u")
    assert _failed(readiness)["tool mcp__*"]["detail"].startswith("profile_disabled:")


def test_skill_mcp_dependencies_are_bound_by_a_wildcard(live, monkeypatch):
    class _Sm:
        def __init__(self, *_a, **_k):
            pass

        def load(self, owner=None):
            return [{"name": "todoist-planning", "requires_toolsets": ["todoist"]}]

    monkeypatch.setattr("services.memory.skills.SkillsManager", _Sm)
    monkeypatch.setattr("src.skill_toolsets.skill_declared_tools",
                        lambda skills, disabled, manager: ({"mcp__todoist__add_task"}, set()))
    monkeypatch.setattr("src.tool_utils.get_mcp_manager", lambda: None)

    for enabled in (("mcp__*",), ("mcp__todoist__*",)):
        readiness = profile_readiness(
            _profile("read_file", *enabled, skill_access="selected", skill_names=["todoist-planning"]),
            _policy(), "u")
        assert readiness["status"] == "READY", render(readiness)

    readiness = profile_readiness(
        _profile("read_file", "mcp__penpot__*", skill_access="selected", skill_names=["todoist-planning"]),
        _policy(), "u")
    assert "does not bind: mcp__todoist__add_task" in _failed(readiness)["skill todoist-planning"]["detail"]
