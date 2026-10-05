from src.tool_selection import plan_tool_selection


def test_denial_and_positive_allowlist_win_over_every_source():
    result = plan_tool_selection(
        {"read", "write", "private"},
        {"explicit": {"write", "private", "missing"}, "semantic": {"read"}},
        disabled_tools={"private"}, allowed_tools={"read", "private"},
    )
    assert result.selected == ("read",)
    assert result.blocked == ("private", "write")
    assert result.unknown == ("missing",)


def test_soft_relevance_exclusion_does_not_revoke_discovery():
    result = plan_tool_selection(
        {"mail", "code"}, {"semantic": {"mail", "code"}}, soft_excluded={"mail"},
    )
    assert result.selected == ("code",)
    assert result.deferred == ("mail",)
    explicit = plan_tool_selection(
        {"mail"}, {"explicit": {"mail"}}, soft_excluded={"mail"},
    )
    assert explicit.selected == ("mail",)


def test_token_and_count_budgets_do_not_silently_drop_explicit_bindings():
    result = plan_tool_selection(
        {"a", "b", "c"}, {"profile": {"b", "a"}, "semantic": {"c"}},
        max_tools=1, schema_costs={"a": 20, "b": 20, "c": 10}, max_schema_tokens=30,
    )
    assert result.selected == ("a", "b")
    assert result.deferred == ("c",)
    assert result.estimated_schema_tokens == 40
    assert result.budget_exceeded_by_explicit


def test_large_catalog_has_stable_bounded_selection_without_mutating_candidates():
    inventory = [f"tool_{n:03}" for n in range(300)]
    candidates = {"semantic": set(inventory), "core": {"tool_299"}}
    first = plan_tool_selection(inventory, candidates, max_tools=8,
                                schema_costs=dict.fromkeys(inventory, 100), max_schema_tokens=500)
    second = plan_tool_selection(reversed(inventory), dict(reversed(list(candidates.items()))),
                                 max_tools=8, schema_costs=dict.fromkeys(inventory, 100), max_schema_tokens=500)
    assert first.selected == second.selected
    assert len(first.selected) == 5 and first.estimated_schema_tokens == 500
    assert "tool_299" in first.selected
    assert len(candidates["semantic"]) == 300
    assert len(first.deferred) == 295
    assert "query" not in first.trace()


def test_empty_selected_allowlist_is_not_unrestricted():
    assert plan_tool_selection({"read"}, {"core": {"read"}}, allowed_tools=set()).selected == ()


def test_connected_catalog_is_discoverable_not_eager_without_another_signal():
    plan = plan_tool_selection(
        {"core", "mcp__weather__forecast"},
        {"core": {"core"}, "connected": {"mcp__weather__forecast"}},
    )
    assert plan.selected == ("core",)
    assert plan.deferred == ("mcp__weather__forecast",)

    relevant = plan_tool_selection(
        {"core", "mcp__weather__forecast"},
        {
            "core": {"core"},
            "connected": {"mcp__weather__forecast"},
            "semantic": {"mcp__weather__forecast"},
        },
    )
    assert relevant.selected == ("core", "mcp__weather__forecast")
