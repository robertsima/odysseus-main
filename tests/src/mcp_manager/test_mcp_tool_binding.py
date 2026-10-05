"""Regression tests for the Penpot MCP tool-binding incident.

Root cause: ``_tool_schemas_for_round`` filtered EVERY MCP tool schema
through the RAG-selected ``relevant_tools`` set whenever that set was
non-empty. ``relevant_tools`` is a semantic-retrieval heuristic sized for
Odysseus's ~100 builtin tools; it was never a reliable gate for a small,
user-configured external MCP server. A connected server's tools (e.g.
Penpot's ``execute_code``) silently vanished from the schema on any turn
whose wording didn't happen to score well against the tool index --
including every "refreshed"/follow-up turn in a conversation, since RAG
retrieval reruns per turn from the message text alone. Only the always-
visible ``manage_mcp`` admin wrapper survived, so the model reported the
real tools "unavailable" even though the server was connected the whole
time.

Fix: MCP tool schemas are split into a small ``mcp_gated_names`` set (the
large embedded catalogs -- browser, GitHub, Todoist, ... -- that legitimately
need RAG/intent gating to avoid flooding a small model's schema list) and
everything else, which now binds unconditionally once a server is connected
and enabled, independent of retrieval, round number, or turn-to-turn state.
"""

from src.mcp_manager import McpManager


PENPOT_TOOLS = ["execute_code", "get_page", "list_boards", "export_asset", "set_layer"]


def _names(schemas):
    return {s["function"]["name"] for s in schemas if s.get("function")}


# ── 2. Reconnect/refresh rebinds connected tools ───────────────────────────


def test_reconnect_rebinds_freshly_discovered_tools():
    mgr = McpManager()
    mgr._tools = {"penpot": [{"name": "execute_code", "description": "run", "input_schema": {}}]}
    mgr._connections = {"penpot": {"status": "connected", "name": "Penpot", "identity": ""}}

    first = _names(mgr.get_all_openai_schemas())
    assert first == {"mcp__penpot__execute_code"}

    # Simulate a reconnect that (re)discovers a fuller tool list -- e.g. the
    # server was restarted after an update, or the very first connect raced
    # ahead of a slow handshake and only partially listed tools.
    mgr._tools["penpot"] = [
        {"name": n, "description": n, "input_schema": {}} for n in PENPOT_TOOLS
    ]
    second = _names(mgr.get_all_openai_schemas())
    assert second == {f"mcp__penpot__{n}" for n in PENPOT_TOOLS}

    # And the new tools are immediately eligible for unconditional binding --
    # not gated -- exactly like the original set was.
    assert mgr.gated_tool_names() == set()


def test_refreshed_turn_recomputes_schemas_from_live_manager_state():
    # A "refreshed" turn calls get_all_openai_schemas() again from scratch
    # (see _build_system_prompt); it must reflect whatever the manager
    # currently knows, not a stale snapshot from a previous turn.
    mgr = McpManager()
    mgr._tools = {}
    mgr._connections = {}
    assert mgr.get_all_openai_schemas() == []

    mgr._tools = {"penpot": [{"name": "execute_code", "description": "run", "input_schema": {}}]}
    mgr._connections = {"penpot": {"status": "connected", "name": "Penpot", "identity": ""}}
    assert _names(mgr.get_all_openai_schemas()) == {"mcp__penpot__execute_code"}


# ── 4. The always-bound guarantee is bounded by size ───────────────────────
#
# Follow-up incident (2026-09-16): the guarantee above was written for "a
# handful of tools" and had no upper bound, so five connected servers put 37
# unselected schemas (11,146 tokens) into every round. Worse than the cost, it
# misrouted the turn -- the request's own tools had not been selected, so those
# 37 strangers were the only actionable things in the list and the agent spent
# six rounds in a design tool and a docs lookup on a "read the logs" request.
#
# The rule now: a server over the per-server cap, or the servers that push the
# total over the total cap (largest first), are gated like the builtin catalogs
# instead of bound unconditionally. The tests above must keep passing unchanged
# -- a small connected server still survives a RAG miss.

import src.mcp_manager as mm



def _server(server_id: str, name: str, count: int) -> dict:
    return {
        server_id: [
            {"name": f"{name}_t{i}", "description": f"{name} tool {i}", "input_schema": {}}
            for i in range(count)
        ]
    }


def _mgr(**servers) -> McpManager:
    """servers: {server_id: (display_name, tool_count)}"""
    mgr = McpManager()
    mgr._tools = {}
    mgr._connections = {}
    for server_id, (name, count) in servers.items():
        mgr._tools.update(_server(server_id, name, count))
        mgr._connections[server_id] = {"status": "connected", "name": name, "identity": ""}
    return mgr


def test_per_server_cap_boundary_is_inclusive():
    # "At the cap" is still a handful; one over is not. Pinned so a later
    # off-by-one cannot silently change which real servers stay bound.
    cap = mm._MCP_ALWAYS_BOUND_SERVER_MAX_TOOLS
    assert _mgr(srv=("srv", cap)).demoted_servers() == []
    assert [row[0] for row in _mgr(srv=("srv", cap + 1)).demoted_servers()] == ["srv"]


def test_demotion_order_is_stable_for_equally_sized_servers():
    # The schema list is part of the cached prompt prefix; a set-iteration
    # tie-break would reshuffle it between rounds and invalidate that cache.
    mgr = _mgr(bbb=("bbb", 7), aaa=("aaa", 7), ccc=("ccc", 7), ddd=("ddd", 7))
    first = [row[0] for row in mgr.demoted_servers()]
    assert first == [row[0] for row in mgr.demoted_servers()]
    assert first == ["aaa"]  # 28 tools -> drop one; alphabetical tie-break


def test_small_server_prompt_section_is_unchanged():
    mgr = _mgr(penpot=("penpot", 5))
    text = mgr.get_tool_descriptions_for_prompt()
    assert "not attached this turn" not in text


def test_builtin_catalogs_are_gated_but_never_reported_as_demoted():
    # They were already behind retrieval before any budget existed, so nothing
    # changed for them; reporting them as demoted would make the operator log
    # line noise on every single turn.
    mgr = _mgr(builtin_browser=("Browser", 30))
    assert mgr.demoted_servers() == []
    assert len(mgr.gated_tool_names()) == 30


def test_disabled_tools_do_not_count_against_the_budget():
    # A big server with most of its tools switched off costs almost nothing in
    # schema tokens, so it keeps the always-bound guarantee.
    mgr = _mgr(firecrawl=("firecrawl", 27))
    disabled = {"firecrawl": {f"firecrawl_t{i}" for i in range(22)}}

    assert [row[0] for row in mgr.demoted_servers()] == ["firecrawl"]  # raw count
    assert mgr.demoted_servers(disabled) == []  # 5 enabled -> under the cap
    assert mgr.gated_tool_names(disabled) == set()


def test_caps_are_settings_overridable_and_zero_means_unbounded(monkeypatch):
    mgr = _mgr(firecrawl=("firecrawl", 27))
    assert [row[0] for row in mgr.demoted_servers()] == ["firecrawl"]

    # 0 restores the pre-fix "bind everything the user connected" behaviour.
    monkeypatch.setattr(mm, "_always_bound_limits", lambda: (0, 0))
    assert mgr.demoted_servers() == []
    assert mgr.gated_tool_names() == set()


def test_bad_setting_values_fall_back_to_the_defaults_instead_of_raising(monkeypatch):
    # A hand-edited settings.json must not take the turn down, and must not
    # accidentally read as "no cap".
    import src.settings as settings_mod

    original = settings_mod.get_setting
    monkeypatch.setattr(
        settings_mod,
        "get_setting",
        lambda key, default=None: (
            "lots" if str(key).startswith("mcp_always_bound") else original(key, default)
        ),
    )
    assert mm._always_bound_limits() == (
        mm._MCP_ALWAYS_BOUND_SERVER_MAX_TOOLS,
        mm._MCP_ALWAYS_BOUND_TOTAL_MAX_TOOLS,
    )


# ── Section 5: a gated catalog is not advertised as callable ────────────────
#
# The prompt lists every connected server's tools under "you also have access
# to these". For a gated server that is only half true: the tools exist, but no
# call schema is attached until retrieval surfaces one. Telling a model a tool
# is callable when it is not is what produces the "I do not have X" refusals
# the self-unblock then has to rescue. The note was added for newly demoted
# servers; the builtin catalogs were gated all along and had the same gap.

def test_a_builtin_catalog_says_its_schemas_are_not_attached():
    text = _mgr(builtin_browser=("browser", 12)).get_tool_descriptions_for_prompt()
    assert "CONNECTED AND WORKING" in text
    assert "attached on demand" in text
    assert "12 call schemas are not attached this turn" in text


def test_a_demoted_server_keeps_its_own_reason():
    text = _mgr(fc=("firecrawl", 27)).get_tool_descriptions_for_prompt()
    assert "too large to attach to every turn" in text
    assert "attached on demand" not in text


def test_a_small_user_server_gets_no_note():
    """It really is attached every turn — saying otherwise would be the lie in
    the other direction."""
    text = _mgr(ntfy=("ntfy", 2)).get_tool_descriptions_for_prompt()
    assert "CONNECTED AND WORKING" not in text


def test_a_gated_catalog_still_lists_its_tools():
    """The note explains the missing schema; it must not replace the listing,
    or the model cannot learn the tools exist and the index loses its corpus."""
    text = _mgr(builtin_browser=("browser", 12)).get_tool_descriptions_for_prompt()
    assert "browser_t0" in text and "browser_t11" in text
