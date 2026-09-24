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


# ── A large catalog asked "does it work?" (2026-09-24, Penpot) ─────────────
#
# Name -> required input params, a subset of the real 81-tool
# @zcubekr/penpot-mcp-server catalog. It declares no annotations, so the name
# heuristic decides what reads. No tool name overlaps a health-check request,
# so the 8 slots used to go alphabetically to get_comment_thread..get_profile
# and cut list_teams, the one call that proves the server reaches Penpot.
PENPOT = "c5ec6d7a"
PENPOT_TOOLS = {
    "get_comment_thread": ["fileId", "threadId"],
    "get_comments": ["threadId"],
    "get_file": ["fileId"],
    "get_file_libraries": ["fileId"],
    "get_library_file_references": ["libraryId"],
    "get_library_usage": ["libraryId"],
    "get_page_shapes": ["fileId", "pageId"],
    "get_profile": [],
    "get_team": [],
    "get_team_stats": ["teamId"],
    "list_comment_threads": [],
    "list_files": ["projectId"],
    "list_font_variants": [],
    "list_projects": ["teamId"],
    "list_teams": [],
    "search_files": ["projectId", "query"],
    "create_team": ["name"],
    "update_team": ["teamId", "name"],
    "delete_team": ["teamId"],
    "delete_team_invitation": ["teamId", "email"],
    "leave_team": ["teamId"],
    "ignore_file_library_sync_status": ["fileId", "date"],
}
PENPOT_WRITES = {
    "create_team", "update_team", "delete_team", "delete_team_invitation",
    "leave_team", "ignore_file_library_sync_status",
}


@pytest.fixture
def penpot():
    manager = McpManager()
    manager._connections[PENPOT] = {"status": "connected", "name": "Penpot"}
    manager._tools[PENPOT] = [
        {
            "name": name,
            "description": name.replace("_", " "),
            "annotations": None,
            "input_schema": {
                "type": "object",
                "properties": {param: {"type": "string"} for param in required},
                "required": required,
            },
        }
        for name, required in PENPOT_TOOLS.items()
    ]
    return manager


@pytest.mark.parametrize("query", ["check that penpot works", "test the penpot mcp server"])
def test_health_check_of_a_large_catalog_gets_its_argument_free_reads(penpot, query):
    picked = penpot.discover_requested_tools(query)
    assert len(picked) == 8
    assert {qualified("get_profile", PENPOT), qualified("list_teams", PENPOT)} <= picked
    # Every read callable without an id the model does not have yet wins a slot...
    assert {qualified(name, PENPOT) for name, required in PENPOT_TOOLS.items() if not required} <= picked
    # ...and a mention alone still never promotes a write.
    assert not picked & {qualified(name, PENPOT) for name in PENPOT_WRITES}


def test_exact_tool_names_still_outrank_argument_free_reads(penpot):
    for query, wanted in (
        (f"check penpot with {qualified('get_team_stats', PENPOT)}", "get_team_stats"),
        ("penpot get_page_shapes", "get_page_shapes"),
    ):
        assert penpot.discover_requested_tools(query, max_tools=1) == {qualified(wanted, PENPOT)}, query


def test_dispatcher_injected_args_do_not_count_as_required(penpot):
    # The model never supplies an _odysseus_ arg, so it does not make a tool
    # any harder to call.
    penpot._tools[PENPOT].append({
        "name": "get_account",
        "input_schema": {
            "type": "object",
            "properties": {"_odysseus_owner": {"type": "string"}},
            "required": ["_odysseus_owner"],
        },
    })
    assert penpot.discover_requested_tools("check that penpot works", max_tools=6) == {
        qualified(name, PENPOT) for name in (
            "get_account", "get_profile", "get_team", "list_comment_threads",
            "list_font_variants", "list_teams",
        )
    }


def test_builtin_python_tools_are_not_promoted_as_native_schemas(bluesky):
    bluesky._connections["memory"] = {"status": "connected", "name": "Memory"}
    bluesky._tools["memory"] = [{"name": "search"}]
    assert bluesky.discover_requested_tools("Use Memory search") == set()


# ── "browser" is a word, not a request for the builtin browser ─────────────
#
# builtin_browser minus the generic "builtin" is "browser", so "my browser
# keeps crashing" named it. Playwright annotates its snapshot/screenshot tools
# read-only, and in the agent loop any browser name makes
# `_expand_browser_mcp_tools` attach the whole catalogue, click/evaluate
# included (review of the 2026-09-24 re-port).
PLAYWRIGHT_READS = {"browser_snapshot", "browser_take_screenshot", "browser_console_messages"}
PLAYWRIGHT_WRITES = {"browser_click", "browser_evaluate", "browser_navigate"}


@pytest.fixture
def with_browser(penpot):
    penpot._connections["builtin_browser"] = {"status": "connected", "name": "Built-in: Browser"}
    penpot._tools["builtin_browser"] = [
        {"name": name, "annotations": {"readOnlyHint": name in PLAYWRIGHT_READS}}
        for name in sorted(PLAYWRIGHT_READS | PLAYWRIGHT_WRITES)
    ]
    return penpot


@pytest.mark.parametrize("query", [
    "my browser keeps crashing",
    "Explain how a browser renders CSS",
    "Use the builtin browser",
])
def test_the_word_browser_does_not_name_the_builtin_browser(with_browser, query):
    assert with_browser.discover_requested_tools(query) == set()


def test_browser_tools_do_not_take_a_named_servers_slots(with_browser):
    picked = with_browser.discover_requested_tools("check penpot in the browser")
    assert {qualified("get_profile", PENPOT), qualified("list_teams", PENPOT)} <= picked
    assert not any(name.startswith("mcp__builtin_browser__") for name in picked)


def test_an_exact_browser_tool_name_still_counts(with_browser):
    name = qualified("browser_snapshot", "builtin_browser")
    assert with_browser.discover_requested_tools(f"Use {name}") == {name}
