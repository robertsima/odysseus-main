import asyncio
import json
import time
from types import SimpleNamespace

from src.tool_discovery import TurnToolDiscovery, _compact_schema_cost
from src import tool_execution
from src.tool_parsing import parse_tool_blocks, strip_tool_blocks


def _no_security_context():
    # Looked up at call time: other tests reload src.tool_execution, which
    # replaces the sentinel execute_tool_block compares by identity.
    import src.tool_execution as tool_execution

    return tool_execution.NO_TOOL_SECURITY_CONTEXT


def schema(name, description="", annotations=None):
    item = {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": {}},
    }}
    if annotations is not None:
        item["function"]["annotations"] = annotations
    return item


CATALOG = [
    schema("web_search", "Search the public web"),
    schema("manage_memory", "Store or search memories"),
    schema("list_models", "List available models"),
    schema("manage_skills", "Inspect or change skills"),
    schema("mcp__social__get-profile", "Read a social profile", {"readOnlyHint": True}),
    schema("mcp__social__create-post", "Create a social post", {"readOnlyHint": False}),
    schema("mcp__private__read-journal", "Read private journal", {"readOnlyHint": True}),
]

PENPOT_SID = "c5ec6d7a"


def penpot(tool, description, required=(), optional=()):
    """A Penpot tool the way McpManager.get_all_openai_schemas emits it: the
    server id in the name, the display name only as a description prefix, and
    no MCP annotations (@zcubekr/penpot-mcp-server declares none)."""
    properties = {name: {"type": "string"} for name in (*required, *optional)}
    return {"type": "function", "function": {
        "name": f"mcp__{PENPOT_SID}__{tool}",
        "description": f"[MCP:Penpot] {description}",
        "parameters": {"type": "object", "properties": properties, "required": list(required)},
    }}


def pp(tool):
    return f"mcp__{PENPOT_SID}__{tool}"


# A slice of the real 81-tool server (2026-09-24), descriptions and required
# arguments as the package ships them.
PENPOT = [
    penpot("get_profile", "Get current user profile"),
    penpot("list_teams", "List all teams the user has access to"),
    penpot("get_team", "Get detailed information about a specific team (provide either teamId or fileId)",
           optional=("teamId", "fileId")),
    penpot("list_comment_threads", "List all comment threads in a file or team (provide either fileId or teamId)",
           optional=("fileId", "teamId", "shareId")),
    penpot("list_projects", "List all projects in a team", ("teamId",)),
    penpot("get_team_members", "Get list of all members in a team", ("teamId",)),
    penpot("list_files", "List all files in a project", ("projectId",)),
    penpot("get_file", "Get detailed information about a file", ("fileId",)),
    penpot("has_file_libraries", "Check if a file uses any component libraries (returns boolean)", ("fileId",)),
    penpot("update_team", "Update team information (name)", ("teamId", "name")),
    penpot("delete_team", "Delete a team (requires owner permissions)", ("teamId",)),
    penpot("delete_team_invitation", "Delete a pending team invitation by email", ("teamId", "email")),
    penpot("delete_team_member", "Remove a member from a team", ("teamId", "memberId")),
    penpot("leave_team", "Leave a team (current user leaves the team)", ("teamId",), ("reassignTo",)),
    penpot("update_shape", "Update shape properties including position, size, colors, gradients",
           ("fileId", "pageId", "shapeId")),
    penpot("create_rectangle", "Create a rectangle shape with colors, gradients, images, borders",
           ("fileId", "pageId")),
]
# Reads that take no arguments: what "is Penpot working?" should reach first.
PENPOT_PROBES = {pp("get_profile"), pp("list_teams"), pp("get_team"), pp("list_comment_threads")}
PENPOT_WRITES = {pp(name) for name in (
    "update_team", "delete_team", "delete_team_invitation", "delete_team_member",
    "leave_team", "update_shape", "create_rectangle",
)}


def run(discovery, query, **kwargs):
    return asyncio.run(discovery.discover(query, **kwargs))


def test_exact_and_token_fallback_is_bounded_stable_and_no_zero_match_flood():
    discovery = TurnToolDiscovery(CATALOG)
    result = run(discovery, "find web_search for web research", max_results=1)
    assert result["loaded_names"] == ["web_search"]
    assert len(result["loaded_tools"]) == 1
    assert run(discovery, "completely unrelated aardvark")["loaded_names"] == []


def test_conservative_description_fallback_requires_two_content_tokens():
    catalog = [
        schema("lookup", "Inspect public web sources"),
        schema("other", "Inspect private database records"),
    ]
    assert run(TurnToolDiscovery(catalog), "review public sources")["loaded_names"] == ["lookup"]
    # One generic description token is insufficient and leaks no candidate.
    assert run(TurnToolDiscovery(catalog), "inspect unrelated material")["loaded_names"] == []


def test_returns_only_newly_loaded_schema_copies():
    discovery = TurnToolDiscovery(CATALOG)
    first = run(discovery, "web search")
    first["loaded_tools"][0]["function"]["description"] = "mutated"
    second = run(discovery, "web search")
    assert second["loaded_names"] == []
    assert discovery.loaded_tools[0]["function"]["description"] == "Search the public web"


def test_exact_name_discovery_needs_no_semantic_request():
    calls = []
    def semantic(query, limit):
        calls.append(query)
        return ["list_models"]
    result = run(TurnToolDiscovery(CATALOG, semantic_search=semantic), "web_search")
    assert result["loaded_names"] == ["web_search"]
    assert calls == []


def test_budget_exhaustion_is_not_reported_as_a_missing_capability():
    result = run(TurnToolDiscovery(CATALOG, max_schema_tokens=1), "web_search")
    assert result["loaded_names"] == []
    assert result["discovery"]["budget_limited"] is True
    assert "exceed this turn's tool budget" in result["output"]


def test_attached_tools_are_reported_without_spending_the_discovery_allowance():
    discovery = TurnToolDiscovery(CATALOG, max_loaded=1, max_schema_tokens=10_000)
    discovery.set_attached(["web_search"])
    result = run(discovery, "web search")
    assert result["loaded_names"] == []
    assert result["already_attached_names"] == ["web_search"]
    assert "already available" in result["output"]
    assert discovery.loaded_names == set()
    assert discovery.loaded_schema_tokens == 0
    # The attached tool is not discovery's to count: the one slot is still free.
    assert run(discovery, "manage memory")["loaded_names"] == ["manage_memory"]
    # ...and what discovery loaded does spend it.
    spent = run(discovery, "list models")
    assert spent["loaded_names"] == []
    assert spent["discovery"]["budget_limited"] is True


def test_set_attached_replaces_snapshot_and_schema_allowance_blocks_autoadd():
    discovery = TurnToolDiscovery(CATALOG, max_loaded=2, max_schema_tokens=1)
    discovery.set_attached(["manage_memory"])
    attached = run(discovery, "manage memory")
    assert attached["loaded_names"] == []
    assert attached["already_attached_names"] == ["manage_memory"]
    discovery.set_attached([])
    # The replaced snapshot no longer reports it, and the one-token schema
    # allowance is too small for discovery to load it instead.
    replaced = run(discovery, "manage memory")
    assert replaced["loaded_names"] == []
    assert replaced["already_attached_names"] == []
    assert replaced["discovery"]["budget_limited"] is True


def test_cumulative_schema_token_budget_uses_compact_cost_and_keeps_canonical_output():
    verbose = schema("verbose_reader", "Read verbose records")
    verbose["function"]["parameters"]["properties"] = {
        "query": {"type": "string", "description": "x" * 4000},
    }
    # Nested prose is removed for payload-cost estimation, so this canonical
    # schema fits and its full contract is still returned unchanged.
    discovery = TurnToolDiscovery([verbose], max_schema_tokens=120)
    result = run(discovery, "verbose reader")
    assert result["loaded_names"] == ["verbose_reader"]
    assert result["loaded_tools"][0]["function"]["parameters"]["properties"]["query"]["description"] == "x" * 4000
    assert discovery.loaded_schema_tokens <= 120

    too_small = TurnToolDiscovery([verbose], max_schema_tokens=1)
    assert run(too_small, "verbose reader")["loaded_names"] == []
    assert too_small.loaded_names == set()


def test_schema_budget_estimator_covers_actual_compact_payload_accounting():
    from src.tool_schemas import compact_function_tool_schemas

    tools = [schema("alpha_reader", "Read alpha records"), schema("beta_reader", "Read beta records")]
    discovery = TurnToolDiscovery(tools, max_schema_tokens=10_000)
    result = run(discovery, "alpha reader beta reader", max_results=2)
    compact = compact_function_tool_schemas(result["loaded_tools"])
    encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    actual_accounted = max(1, int(len(encoded) * 0.3))
    assert discovery.loaded_schema_tokens >= actual_accounted


def test_constructor_disabled_and_positive_ceiling_filter_before_output():
    discovery = TurnToolDiscovery(
        CATALOG, disabled_tools={"manage_memory"}, allowed_tools={"web_search", "manage_memory"},
    )
    assert run(discovery, "manage memory")["loaded_names"] == []
    assert run(discovery, "social profile")["loaded_names"] == []
    inventory = discovery.permitted_tools()
    assert [item["function"]["name"] for item in inventory] == ["web_search"]
    inventory[0]["function"]["description"] = "changed"
    assert discovery.permitted_tools()[0]["function"]["description"] == "Search the public web"
    assert discovery.loaded_names == set()


def test_forbidden_semantic_hits_are_filtered_before_any_output():
    async def semantic(_query, _limit):
        return ["manage_memory", "web_search"]

    discovery = TurnToolDiscovery(
        CATALOG, disabled_tools={"manage_memory"}, semantic_search=semantic,
    )
    result = run(discovery, "find capability")
    assert result["loaded_names"] == ["web_search"]
    assert "manage_memory" not in repr(result)


def test_none_and_selected_profiles_and_email_aliases():
    email = schema("mcp__email__read_email", "Read email", {"readOnlyHint": True})
    assert run(TurnToolDiscovery(CATALOG), "web search", settings={"tool_access": "none"})["loaded_names"] == []
    selected = TurnToolDiscovery(CATALOG + [email])
    result = run(selected, "read email", settings={
        "tool_access": "selected", "enabled_tools": ["read_email"],
    })
    assert result["loaded_names"] == ["mcp__email__read_email"]
    assert run(TurnToolDiscovery(CATALOG), "web search", settings={
        "tool_access": "selected", "enabled_tools": ["manage_memory"],
    })["loaded_names"] == []


def test_mcp_server_allowlist_and_runtime_revocation_are_fresh():
    discovery = TurnToolDiscovery(CATALOG)
    denied = run(discovery, "social profile", settings={"allowed_mcp_servers": []})
    assert denied["loaded_names"] == []
    allowed = run(discovery, "social profile", settings={"allowed_mcp_servers": ["social"]})
    assert allowed["loaded_names"] == ["mcp__social__get-profile"]
    other = TurnToolDiscovery(CATALOG)
    revoked = run(other, "web search", settings={"_runtime_disabled_tools": ["web_search"]})
    assert revoked["loaded_names"] == []


def test_fresh_settings_disabled_alias_filters_before_discovery_output():
    email = schema("mcp__email__read_email", "Read private email", {"readOnlyHint": True})
    discovery = TurnToolDiscovery([email, schema("web_search", "Search public web")])
    result = run(discovery, "read private email", settings={"disabled_tools": ["read_email"]})
    assert result["loaded_names"] == []
    assert result["already_attached_names"] == []
    assert "read_email" not in repr(result)
    assert result["loaded_tools"] == []
    assert result["discovery"]["tools"] == []


def test_memory_model_skill_and_readonly_filters():
    settings = {"memory_access": "none", "model_access": "current", "skill_access": "none"}
    discovery = TurnToolDiscovery(CATALOG)
    for query in ("manage memory", "list models", "manage skills"):
        assert run(discovery, query, settings=settings)["loaded_names"] == []
    readonly = TurnToolDiscovery(CATALOG)
    assert run(readonly, "social create post", settings={"workflow_readonly": True})["loaded_names"] == []
    assert run(readonly, "social get profile", settings={"workflow_readonly": True})["loaded_names"] == [
        "mcp__social__get-profile"
    ]
    skills = TurnToolDiscovery(CATALOG)
    assert run(skills, "manage skills", settings={
        "tool_access": "selected", "enabled_tools": ["manage_skills"],
        "skill_access": "selected", "skill_names": [],
    })["loaded_names"] == []
    assert run(TurnToolDiscovery(CATALOG), "manage skills", settings={
        "tool_access": "selected", "enabled_tools": ["manage_skills"],
        "skill_access": "selected", "skill_names": ["research"],
        "workflow_readonly": True,
    })["loaded_names"] == ["manage_skills"]


def test_private_unconfined_tools_are_not_advertised_but_public_file_tools_remain():
    names = ["bash", "python", "mcp__files__read_file", "read_file", "grep"]
    catalog = [schema(name, "Read repository files") for name in names]
    discovery = TurnToolDiscovery(catalog)
    assert {s["function"]["name"] for s in discovery.permitted_tools()} == {"read_file", "grep"}
    assert run(discovery, "bash")["loaded_names"] == []
    settings = {"private_vault_access": True}
    assert {s["function"]["name"] for s in discovery.permitted_tools(settings)} == set(names)
    assert run(discovery, "bash", settings=settings)["loaded_names"] == ["bash"]
    # Loaded/attached schemas are still hidden immediately after revocation.
    discovery.set_attached(["bash"])
    assert run(discovery, "bash", settings={"private_vault_access": False})["already_attached_names"] == []


def test_discovery_dispatch_does_not_escalate_from_fresh_grant(monkeypatch):
    discovery = TurnToolDiscovery([schema("bash", "Run shell")])
    _, result = _execute(monkeypatch, discovery, {"private_vault_access": True}, {"query": "bash"})
    assert result["loaded_names"] == []


def test_semantic_timeout_falls_back_and_result_boundaries():
    async def slow(_query, _limit):
        await asyncio.sleep(2.2)
        return ["manage_memory"]

    discovery = TurnToolDiscovery(CATALOG, semantic_search=slow)
    started = time.monotonic()
    result = run(discovery, "web search", max_results=99)
    assert time.monotonic() - started < 2.15
    assert result["loaded_names"] == ["web_search"]
    assert result["max_results"] == 8
    assert result["discovery"]["loaded_names"] == ["web_search"]
    assert run(TurnToolDiscovery(CATALOG), "x" * 600)["discovery"]["query"] == "x" * 500


def _execute(monkeypatch, discovery, settings, args=None):
    monkeypatch.setattr("core.database.get_session_settings", lambda sid, **kwargs: dict(settings))
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    block = SimpleNamespace(tool_type="discover_tools", content=json.dumps(args or {"query": "web search"}))
    return asyncio.run(tool_execution.execute_tool_block(
        block, session_id="session", owner="owner", tool_discovery=discovery,security_context=_no_security_context()
    ))


def test_dispatcher_requires_context_and_validates_arguments(monkeypatch):
    desc, result = _execute(monkeypatch, None, {})
    assert "BLOCKED" in desc and result["exit_code"] == 1
    for args in ({"query": ""}, {"query": "x" * 501}, {"query": "x", "max_results": 0}):
        _, result = _execute(monkeypatch, TurnToolDiscovery(CATALOG), {}, args)
        assert result["exit_code"] == 1


def test_dispatcher_selected_nonempty_can_discover_only_selection(monkeypatch):
    discovery = TurnToolDiscovery(CATALOG)
    _, result = _execute(monkeypatch, discovery, {
        "tool_access": "selected", "enabled_tools": ["web_search"],
    })
    assert result["loaded_names"] == ["web_search"]
    _, blocked = _execute(monkeypatch, TurnToolDiscovery(CATALOG), {
        "tool_access": "none", "enabled_tools": ["web_search"],
    })
    assert blocked["exit_code"] == 1


def test_dispatcher_fresh_revocation_blocks_actual_tool_before_handler(monkeypatch):
    monkeypatch.setattr("core.database.get_session_settings", lambda sid, **kwargs: {
        "disabled_tools": ["read_file"],
    })
    handler = __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock()
    monkeypatch.setattr(tool_execution, "_direct_fallback", handler)
    block = SimpleNamespace(tool_type="read_file", content=json.dumps({"path": "README.md"}))
    desc, result = asyncio.run(tool_execution.execute_tool_block(
        block, session_id="session", owner="owner", disabled_tools=set(),security_context=_no_security_context()
    ))
    assert "BLOCKED" in desc
    assert result["blocked_reason"] == "fresh_session_disabled"
    handler.assert_not_awaited()


def test_dispatcher_fresh_revocation_blocks_discovery_context(monkeypatch):
    discovery = TurnToolDiscovery(CATALOG)
    desc, result = _execute(monkeypatch, discovery, {"disabled_tools": ["discover_tools"]})
    assert "BLOCKED" in desc
    assert result["blocked_reason"] == "fresh_session_disabled"
    assert discovery.loaded_names == set()


def test_dynamic_mcp_fence_strictly_accepts_only_attached_names():
    allowed = "mcp__social__get-profile"
    denied = "mcp__social__delete-profile"
    text = f"```{allowed}\n{{\"id\": \"1\"}}\n```\n```{denied}\n{{\"id\": \"1\"}}\n```"
    blocks = parse_tool_blocks(text, dynamic_tool_names={allowed})
    assert [(b.tool_type, json.loads(b.content)) for b in blocks] == [(allowed, {"id": "1"})]
    cleaned = strip_tool_blocks(text, dynamic_tool_names={allowed})
    assert allowed not in cleaned
    assert denied in cleaned


def test_dynamic_mcp_fence_default_compatibility_and_identifier_bounds():
    name = "mcp__social__get-profile"
    assert parse_tool_blocks(f"```{name}\n{{}}\n```")[0].tool_type == name
    too_long_server = "mcp__" + ("s" * 65) + "__read"
    too_long_tool = "mcp__social__" + ("t" * 129)
    assert parse_tool_blocks(f"```{too_long_server}\n{{}}\n```") == []
    assert parse_tool_blocks(f"```{too_long_tool}\n{{}}\n```") == []
    assert parse_tool_blocks(f"```{name}\n{{}}\n```", dynamic_tool_names=set()) == []


def test_a_policy_dropped_tool_is_reported_as_policy_not_budget():
    """The 2026-09-23 invention. Discovery could only say "No permitted tools
    matched that discovery query", which is true of the permitted inventory and
    says nothing about the tool the caller asked for by name — so the model
    supplied its own cause ("tool discovery hit the schema budget and returned
    no loadout-management tool") and reported the invention to the user as fact.
    """
    loadout = schema("manage_agent_loadout", "Define and start worker loadouts")
    discovery = TurnToolDiscovery(
        CATALOG + [loadout], disabled_tools={"manage_agent_loadout"},
    )
    result = run(discovery, "manage_agent_loadout")
    assert "manage_agent_loadout" not in result["loaded_names"]
    assert result["policy_denied_names"] == ["manage_agent_loadout"]
    # Not a budget outcome, and the result must not let it read as one.
    assert result["discovery"]["budget_limited"] is False
    assert "manage_agent_loadout" in result["output"]
    assert "tool policy" in result["output"]
    assert "not by the schema budget" in result["output"]

    # A real budget stop still says budget, and claims no policy drop.
    budget = run(TurnToolDiscovery(CATALOG, max_schema_tokens=1), "web_search")
    assert budget["discovery"]["budget_limited"] is True
    assert budget["policy_denied_names"] == []
    assert "not by the schema budget" not in budget["output"]


def test_a_denied_tool_the_caller_never_named_is_not_disclosed():
    """The other half: policy honesty must not become a way to enumerate what
    the chat is not allowed to have. Only a name the caller wrote out — which
    it therefore already holds — is repeated back."""
    discovery = TurnToolDiscovery(CATALOG, disabled_tools={"manage_memory"})
    result = run(discovery, "store something for later")
    assert result["policy_denied_names"] == []
    assert "manage_memory" not in repr(result)
    # ...and an empty answer still says which kind of empty it is, so there is
    # no room to invent a budget that was never hit.
    assert "not a schema-budget result" in result["output"]


def test_a_wide_round_still_leaves_discovery_its_own_allowance():
    """The 2026-09-24 Penpot turn. The round already sent 37 schemas, and the
    32-tool / 4096-token ceilings were shared with them, so every discover_tools
    call -- even for the exact name the prompt listed -- came back
    budget_limited and the model told the user its tool budget was exhausted.
    Built like production: the real native registry plus Penpot schemas as
    get_all_openai_schemas emits them, on the class defaults agent_loop uses.
    """
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    native = [s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS if s["function"]["name"] != "discover_tools"]
    sent = native[:35] + [pp("delete_team"), pp("delete_team_invitation")]
    assert len(sent) == 37
    discovery = TurnToolDiscovery(list(FUNCTION_TOOL_SCHEMAS) + PENPOT)
    discovery.set_attached(sent)

    exact = run(discovery, pp("get_profile"))
    assert exact["loaded_names"] == [pp("get_profile")]
    assert exact["discovery"]["budget_limited"] is False
    assert "same turn" in exact["output"]

    discovery.set_attached(sent + [pp("get_profile")])
    named = run(discovery, "penpot get_profile list_teams")
    assert named["loaded_names"][0] == pp("list_teams")
    assert pp("get_profile") in named["already_attached_names"]
    # What the round already sends is never loaded again or charged to discovery.
    assert not set(named["loaded_names"]) & set(sent)
    assert discovery.loaded_names == {pp("get_profile"), *named["loaded_names"]}
    assert discovery.loaded_schema_tokens == sum(
        _compact_schema_cost(item) for item in discovery.loaded_tools
    )

    fresh = TurnToolDiscovery(list(FUNCTION_TOOL_SCHEMAS) + PENPOT)
    fresh.set_attached(sent)
    assert run(fresh, "penpot get_profile list_teams")["loaded_names"][:2] == [pp("get_profile"), pp("list_teams")]


def test_a_budget_stop_is_reported_even_when_other_matches_are_attached():
    """"Matching tools are already available: ..." alone read as the whole
    answer, hiding that the tools actually asked for were withheld."""
    discovery = TurnToolDiscovery(CATALOG, max_loaded=1)
    assert run(discovery, "web search")["loaded_names"] == ["web_search"]
    discovery.set_attached(["web_search", "list_models"])
    result = run(discovery, "list models manage memory")
    assert result["loaded_names"] == []
    assert result["already_attached_names"] == ["list_models"]
    assert result["discovery"]["budget_limited"] is True
    assert "already available: list_models" in result["output"]
    assert "withheld by this turn's discovery budget" in result["output"]
    assert "not a schema-budget result" not in result["output"]
    # The attached match is callable now, so the turn still continues.
    assert result["continue_same_turn"] is True


def test_a_query_naming_the_server_finds_its_reads_first():
    discovery_catalog = CATALOG + PENPOT
    for query in ("penpot", "penpot health check", "check penpot works"):
        result = run(TurnToolDiscovery(discovery_catalog), query, max_results=8)
        loaded = result["loaded_names"]
        assert set(loaded[:4]) == PENPOT_PROBES, query
        assert not set(loaded) & PENPOT_WRITES, query
        assert len(loaded) == 8, query
    # Bounded by max_results even though the query ties the whole server.
    assert len(run(TurnToolDiscovery(discovery_catalog), "penpot", max_results=99)["loaded_names"]) == 8
    assert set(run(TurnToolDiscovery(discovery_catalog), "penpot")["loaded_names"]) >= {
        pp("get_profile"), pp("list_teams"),
    }


def test_a_tool_the_query_describes_outranks_the_servers_reads():
    """Naming the server ties all its tools on the name; the rest of the query
    still picks the tool it describes instead of five unrelated reads."""
    distribute = penpot("distribute_shapes", "Distribute multiple shapes evenly with equal spacing",
                        ("fileId", "pageId", "shapeIds", "direction"))
    discovery_catalog = CATALOG + PENPOT + [distribute]
    assert run(TurnToolDiscovery(discovery_catalog), "penpot equal spacing")["loaded_names"][0] == pp(
        "distribute_shapes"
    )
    # One describing word is not a description match (has_file_libraries
    # says "Check"), so a health check still reaches the reads.
    assert set(run(TurnToolDiscovery(discovery_catalog), "penpot health check")["loaded_names"][:4]) == PENPOT_PROBES


def test_name_tokens_and_exact_names_outrank_the_read_preference():
    discovery_catalog = CATALOG + PENPOT
    assert run(TurnToolDiscovery(discovery_catalog), "penpot delete team")["loaded_names"][0] == pp("delete_team")
    assert run(TurnToolDiscovery(discovery_catalog), "penpot update team")["loaded_names"][0] == pp("update_team")
    assert run(TurnToolDiscovery(discovery_catalog), pp("update_team"))["loaded_names"] == [pp("update_team")]


def test_server_label_matching_ignores_the_account_and_generic_words():
    tool = penpot("get_profile", "Get current user profile")
    tool["function"]["description"] = "[MCP:Penpot MCP Server (robert@example.com)] Get current user profile"
    # Each is one description word (below the two-word fallback), so only a
    # label hit could match: the connected account and "mcp"/"server" name
    # no server.
    for query in ("mcp", "server", "robert"):
        assert run(TurnToolDiscovery([tool]), query)["loaded_names"] == [], query
    assert run(TurnToolDiscovery([tool]), "penpot")["loaded_names"] == [pp("get_profile")]
    # A tool without the manager's prefix gains no label from its prose.
    plain = schema("mcp__x__get_profile", "Penpot profile reader [MCP:Penpot]")
    assert run(TurnToolDiscovery([plain]), "penpot")["loaded_names"] == []


def github_read(tool, description, required=("owner", "repo")):
    """A tool of the builtin GitHub Read server, labelled as the manager
    labels it (src/builtin_mcp.py names the server "Built-in: GitHub Read")."""
    return {"type": "function", "function": {
        "name": f"mcp__github_read__{tool}",
        "description": f"[MCP:Built-in: GitHub Read] {description}",
        "parameters": {"type": "object", "properties": {name: {"type": "string"} for name in required},
                       "required": list(required)},
    }}


GITHUB_READ = [
    github_read("get_me", "Get details of the authenticated GitHub user.", required=()),
    github_read("actions_list", "Tools for listing GitHub Actions resources.", ("method", "owner", "repo")),
    github_read("get_commit", "Get details for a commit from a GitHub repository", ("owner", "repo", "sha")),
    github_read("issue_read", "Get information about a specific issue in a GitHub repository.",
                ("method", "owner", "repo", "issue_number")),
    github_read("list_branches", "List branches in a GitHub repository"),
    github_read("list_commits", "Get list of commits of a branch in a GitHub repository."),
]
DOCUMENT_TOOLS = [
    schema("create_document", "Create a new document in the editor panel."),
    schema("edit_document", "Edit a document open in the editor panel."),
    schema("read_file", "Read a file from disk."),
]


def test_builtin_server_labels_do_not_make_everyday_words_name_every_tool():
    """Counting each label word would make "built" and "read" name hits for
    all of GitHub Read's tools, and "read the document" would load get_me,
    actions_list and get_commit but no document tool."""
    catalog = CATALOG + DOCUMENT_TOOLS + GITHUB_READ
    loaded = run(TurnToolDiscovery(catalog), "read the document", max_results=8)["loaded_names"]
    assert {"create_document", "edit_document", "read_file"} <= set(loaded)
    # Only the GitHub tool whose own name says "read".
    assert [name for name in loaded if name.startswith("mcp__github_read__")] == ["mcp__github_read__issue_read"]
    assert run(TurnToolDiscovery(catalog), "built")["loaded_names"] == []
    # The whole label still names the server.
    named = run(TurnToolDiscovery(catalog), "is github read working", max_results=8)["loaded_names"]
    assert {schema_["function"]["name"] for schema_ in GITHUB_READ} <= set(named)


def test_a_tool_name_hit_outranks_a_server_name_hit():
    loaded = run(TurnToolDiscovery(CATALOG + PENPOT), "list penpot models", max_results=8)["loaded_names"]
    assert loaded[0] == "list_models"
    # list_teams matches a word of its own name; get_profile only the server's.
    assert loaded.index(pp("list_teams")) < loaded.index(pp("get_profile"))


def test_the_description_still_breaks_ties_between_tool_name_hits():
    """Reads-first ahead of the description is for a query that only names a
    server. Past a tool-name hit it would load list_branches and then
    unrelated no-argument list_* tools instead of list_commits."""
    loaded = run(TurnToolDiscovery(CATALOG + GITHUB_READ), "list my github branches")["loaded_names"]
    assert loaded[:2] == ["mcp__github_read__list_branches", "mcp__github_read__list_commits"]
    assert loaded.index("mcp__github_read__list_commits") < loaded.index("list_models")


def test_readonly_workflows_discover_unannotated_mcp_reads_like_the_executor_allows():
    """The executor lets a read-only worker call an unannotated MCP read by its
    name (mcp_call_is_readonly) and plan mode blocks by the same heuristic, but
    discovery failed closed on every unannotated MCP name, so neither could
    ever be offered one."""
    catalog = [
        schema("mcp__penpot__get_profile", "[MCP:Penpot] Get current user profile"),
        schema("mcp__penpot__update_team", "[MCP:Penpot] Update team information (name)"),
    ]
    for settings in ({"workflow_readonly": True}, {"plan_mode": True}):
        discovery = TurnToolDiscovery(catalog)
        assert discovery.permitted_names(settings) == {"mcp__penpot__get_profile"}
        assert run(discovery, "get profile", settings=settings)["loaded_names"] == ["mcp__penpot__get_profile"]
        assert run(discovery, "update team", settings=settings)["loaded_names"] == []
