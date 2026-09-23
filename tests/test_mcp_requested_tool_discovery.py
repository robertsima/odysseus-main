"""Connected catalogs stay discoverable without binding their write tools."""

from types import SimpleNamespace

import pytest

from src.mcp_manager import McpManager, mcp_tool_is_readonly


SERVER = "4dd5076c"
READS = {"get-profile", "get-timeline", "get-post", "get-posts"}
WRITES = {
    "create-post", "delete-post", "like-post", "unlike-post", "repost",
    "follow", "unfollow",
}


def qualified(name, server=SERVER):
    return f"mcp__{server}__{name}"


@pytest.fixture
def bluesky():
    manager = McpManager()
    manager._connections[SERVER] = {"status": "connected", "name": "bluesky-mcp"}
    manager._tools[SERVER] = [
        {
            "name": name,
            "description": f"{name} on Bluesky",
            "annotations": {"readOnlyHint": name in READS},
            "input_schema": {"type": "object", "properties": {}},
        }
        for name in sorted(READS | WRITES)
    ]
    return manager


def test_eleven_tool_bluesky_catalog_selects_only_four_research_tools(bluesky, monkeypatch):
    monkeypatch.setattr("src.mcp_manager._always_bound_limits", lambda: (8, 24))
    # Demotion is gating, not disconnection or removal from the inventory.
    assert len(bluesky.gated_tool_names()) == 11
    assert bluesky.get_server_status(SERVER)["status"] == "connected"
    assert bluesky.discover_requested_tools("Research buyer engagement on Bluesky") == {
        qualified(name) for name in READS
    }


@pytest.mark.parametrize("query", [
    "Use Bluesky MCP server to research Umni",
    "Use bluesky-mcp to research Umni",
    "Use BLUESKY for content research",
    f"Use connected server {SERVER}",
])
def test_server_identity_variants(bluesky, query):
    assert bluesky.discover_requested_tools(query) == {qualified(name) for name in READS}


@pytest.mark.parametrize("query", [
    "Research the market using MCP tools",
    "Which servers are connected?",
    "Get profile and timeline from another tool",
    "Research fakebluesky-mcp-addon",
    "Write ten posts about the sky",
])
def test_unrelated_words_do_not_select_catalog(bluesky, query):
    assert bluesky.discover_requested_tools(query) == set()


def test_fully_qualified_tool_works_without_server_display_name(bluesky):
    assert bluesky.discover_requested_tools(f"Use {qualified('get-post')}") == {
        qualified("get-post")
    }


def test_qualified_name_does_not_match_longer_tool_name_prefix(bluesky):
    bluesky._tools[SERVER].append({"name": "get-post-thread"})
    assert bluesky.discover_requested_tools(f"Use {qualified('get-post-thread')}") == {
        qualified("get-post-thread")
    }


def test_named_tool_has_priority_in_small_budget(bluesky):
    assert bluesky.discover_requested_tools("Read get-timeline from Bluesky", max_tools=1) == {
        qualified("get-timeline")
    }


def test_selected_bindings_survive_unrelated_followup_and_restrict_catalog(bluesky):
    selected = {qualified("get-timeline"), qualified("get-post")}
    assert bluesky.discover_requested_tools("Continue", enabled_tools=selected) == selected
    assert bluesky.discover_requested_tools("Research Bluesky", enabled_tools=selected) == selected
    assert bluesky.discover_requested_tools("Research Bluesky", enabled_tools=set()) == set()


def test_disabled_tools_and_allowlist_win_over_explicit_binding(bluesky):
    requested = {qualified(name) for name in READS}
    args = {"enabled_tools": requested, "allowed_servers": [SERVER]}
    assert bluesky.discover_requested_tools(
        "Bluesky", **args,
        disabled_map={SERVER: {"get-profile"}},
        disabled_tools={qualified("get-post")},
    ) == {qualified("get-timeline"), qualified("get-posts")}
    assert bluesky.discover_requested_tools("Bluesky", allowed_servers=[]) == set()
    assert bluesky.discover_requested_tools("Bluesky", allowed_servers=["different"]) == set()
    assert bluesky.discover_requested_tools("Bluesky", allowed_servers=["*"]) == requested


def test_private_endpoint_filter_cannot_be_bypassed_by_query_or_binding(bluesky):
    # Same map shape agent_loop._apply_private_mcp_filter applies to Lotus.
    bluesky._connections["lotus"] = {"status": "connected", "name": "Lotus"}
    bluesky._tools["lotus"] = [{"name": "read_journal", "description": "Private journal"}]
    private = qualified("read_journal", "lotus")
    assert bluesky.discover_requested_tools(
        f"Research my private Lotus journal with {private}",
        enabled_tools={private}, disabled_map={"lotus": {"read_journal"}},
        disabled_tools={private},
    ) == set()


def test_research_cannot_promote_writes_even_when_mentioned(bluesky):
    text = "Research Bluesky engagement, including create-post, delete-post and " + qualified("like-post")
    assert bluesky.discover_requested_tools(text) == {qualified(name) for name in READS}


def test_readonly_mode_blocks_write_bindings(bluesky):
    selected = {qualified("get-profile"), qualified("create-post")}
    assert bluesky.discover_requested_tools("Continue", enabled_tools=selected) == selected
    assert bluesky.discover_requested_tools(
        "Continue", enabled_tools=selected, readonly=True,
    ) == {qualified("get-profile")}


@pytest.mark.parametrize("status", ["disconnected", "connecting", "error", None])
def test_stale_tools_from_disconnected_server_not_promoted(bluesky, status):
    bluesky._connections[SERVER]["status"] = status
    assert bluesky.discover_requested_tools("Bluesky", enabled_tools={qualified("get-post")}) == set()


def test_annotations_are_authoritative_and_support_pydantic_shape(bluesky):
    bluesky._tools[SERVER] = [
        {"name": "get-delete-token", "annotations": {"readOnlyHint": False}},
        {"name": "get-destructive", "annotations": {"destructiveHint": True}},
        {"name": "inspect-data", "annotations": SimpleNamespace(readOnlyHint=True)},
        {"name": "get-post"},  # Existing conservative name heuristic fallback.
        {"name": "execute-code"},
    ]
    assert bluesky.discover_requested_tools("Research Bluesky") == {
        qualified("inspect-data"), qualified("get-post"),
    }


@pytest.mark.parametrize("annotation_type", [dict, SimpleNamespace])
def test_flat_inventory_preserves_authoritative_readonly_annotations(bluesky, annotation_type):
    bluesky._tools[SERVER] = [
        {"name": "get-reset", "annotations": annotation_type(readOnlyHint=False)},
        {"name": "timeline", "annotations": annotation_type(readOnlyHint=True)},
    ]
    rows = {row["name"]: row for row in bluesky.get_all_tools()}
    assert not mcp_tool_is_readonly(rows["get-reset"])
    assert mcp_tool_is_readonly(rows["timeline"])
    for source in bluesky._tools[SERVER]:
        assert rows[source["name"]]["annotations"] == source["annotations"]


@pytest.mark.parametrize("annotation_type", [dict, SimpleNamespace])
def test_destructive_hint_wins_over_conflicting_readonly_hint(bluesky, annotation_type):
    metadata = {
        "name": "get-and-delete-posts",
        "annotations": annotation_type(readOnlyHint=True, destructiveHint=True),
    }
    bluesky._tools[SERVER] = [metadata]
    assert not mcp_tool_is_readonly(metadata)
    assert not mcp_tool_is_readonly(bluesky.get_all_tools()[0])
    assert bluesky.discover_requested_tools("Research Bluesky") == set()
    assert bluesky.discover_requested_tools(
        "Continue", enabled_tools={qualified(metadata["name"])}, readonly=True,
    ) == set()
    assert qualified(metadata["name"]) in bluesky.plan_mode_blocked_mcp()[1]


def test_many_read_tools_are_bounded_and_ties_stable(bluesky):
    bluesky._tools[SERVER] = [{"name": f"get-item-{n:02}"} for n in range(40)]
    expected = {qualified(f"get-item-{n:02}") for n in range(8)}
    assert bluesky.discover_requested_tools("Research Bluesky") == expected
    bluesky._tools[SERVER].reverse()
    assert bluesky.discover_requested_tools("Research Bluesky") == expected
    assert bluesky.discover_requested_tools("Research Bluesky", max_tools=0) == set()


def test_builtin_python_tools_are_not_promoted_as_native_schemas(bluesky):
    bluesky._connections["memory"] = {"status": "connected", "name": "Memory"}
    bluesky._tools["memory"] = [{"name": "search"}]
    assert bluesky.discover_requested_tools("Use Memory search") == set()
