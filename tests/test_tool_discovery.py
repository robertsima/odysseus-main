import asyncio
import json
import time
from types import SimpleNamespace

from src.tool_discovery import TurnToolDiscovery
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


def test_attached_tools_consume_shared_budgets_without_becoming_discovery_results():
    discovery = TurnToolDiscovery(CATALOG, max_loaded=1, max_schema_tokens=10_000)
    discovery.set_attached(["web_search"])
    result = run(discovery, "web search")
    assert result["loaded_names"] == []
    assert result["already_attached_names"] == ["web_search"]
    assert "already available" in result["output"]
    assert discovery.loaded_names == set()
    assert discovery.loaded_schema_tokens == 0
    # The attached tool consumes the only count slot, so another match cannot load.
    assert run(discovery, "manage memory")["loaded_names"] == []


def test_set_attached_replaces_snapshot_and_schema_budget_blocks_autoadd():
    discovery = TurnToolDiscovery(CATALOG, max_loaded=2, max_schema_tokens=1)
    discovery.set_attached(["web_search"])
    assert run(discovery, "manage memory")["loaded_names"] == []
    discovery.set_attached([])
    # Replacement frees count but the one-token schema ceiling remains too small.
    assert run(discovery, "manage memory")["loaded_names"] == []


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
