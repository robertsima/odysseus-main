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

import src.agent_loop as al
from src.mcp_manager import McpManager


def _mcp_schema(server_id: str, tool_name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": f"mcp__{server_id}__{tool_name}",
            "description": f"[MCP:{server_id}] {tool_name}",
            "parameters": {"type": "object", "properties": {}},
        },
    }


PENPOT_TOOLS = ["execute_code", "get_page", "list_boards", "export_asset", "set_layer"]
PENPOT_SCHEMAS = [_mcp_schema("penpot", name) for name in PENPOT_TOOLS]
BROWSER_SCHEMAS = [_mcp_schema("builtin_browser", name) for name in ("click", "navigate", "screenshot")]


def _round(relevant_tools, mcp_schemas, **overrides):
    kwargs = dict(
        force_answer=False,
        is_api_model=True,
        relevant_tools=relevant_tools,
        needs_admin=False,
        mcp_schemas=mcp_schemas,
        disabled_tools=set(),
        ody_qwen_finetune_model=False,
        last_user="",
    )
    kwargs.update(overrides)
    return al._tool_schemas_for_round(**kwargs)


def _names(schemas):
    return {s["function"]["name"] for s in schemas if s.get("function")}


def _mcp_names(schemas):
    return {n for n in _names(schemas) if n.startswith("mcp__")}


# ── 1. Connected MCP server tools appear in the turn tool schema ──────────


def test_connected_external_mcp_tools_survive_a_rag_miss():
    # The RAG retrieval for this turn's wording didn't surface anything from
    # Penpot -- a vague follow-up like "now run it" is exactly the case that
    # triggered the incident. The connected server's tools must still bind.
    relevant_tools = {"ask_user", "manage_memory"}  # ALWAYS_AVAILABLE-ish, no penpot hits
    mgr = McpManager()
    mgr._tools = {"penpot": [{"name": n, "description": n, "input_schema": {}} for n in PENPOT_TOOLS]}

    selected = _round(
        relevant_tools,
        PENPOT_SCHEMAS,
        mcp_gated_names=mgr.gated_tool_names(),
    )

    assert {"mcp__penpot__execute_code", "mcp__penpot__get_page"} <= _names(selected)


def test_admin_intent_no_longer_hides_the_real_tools_behind_manage_mcp():
    # Reproduces the exact incident shape: the query reads as admin/MCP-ish
    # ("manage my MCP servers" style wording), which pulls manage_mcp into
    # scope, while the actual per-tool schemas from the connected server must
    # ALSO still be present -- not just the management wrapper.
    relevant_tools = {"ask_user"}
    mgr = McpManager()
    mgr._tools = {"penpot": [{"name": n, "description": n, "input_schema": {}} for n in PENPOT_TOOLS]}

    selected = _round(
        relevant_tools,
        PENPOT_SCHEMAS,
        needs_admin=True,
        mcp_gated_names=mgr.gated_tool_names(),
    )

    names = _names(selected)
    assert "manage_mcp" in names  # the wrapper is still there
    assert "mcp__penpot__execute_code" in names  # ...and so is the real tool


def test_large_embedded_catalogs_still_require_retrieval_relevance():
    # Guard against reintroducing the ~30-schema Playwright flood: gated
    # (embedded, large) catalogs must still need a real RAG/intent hit.
    relevant_tools = {"ask_user"}
    mgr = McpManager()
    mgr._tools = {"builtin_browser": [{"name": n, "description": n, "input_schema": {}} for n in ("click", "navigate", "screenshot")]}

    selected = _round(
        relevant_tools,
        BROWSER_SCHEMAS,
        mcp_gated_names=mgr.gated_tool_names(),
    )

    assert _mcp_names(selected) == set()


def test_gated_tool_shows_once_actually_retrieved():
    relevant_tools = {"ask_user", "mcp__builtin_browser__click"}
    mgr = McpManager()
    mgr._tools = {"builtin_browser": [{"name": n, "description": n, "input_schema": {}} for n in ("click", "navigate", "screenshot")]}

    selected = _round(
        relevant_tools,
        BROWSER_SCHEMAS,
        mcp_gated_names=mgr.gated_tool_names(),
    )

    assert _mcp_names(selected) == {"mcp__builtin_browser__click"}


def test_disabled_external_tool_stays_hidden_even_though_unconditionally_bound():
    relevant_tools = {"ask_user"}
    mgr = McpManager()
    mgr._tools = {"penpot": [{"name": n, "description": n, "input_schema": {}} for n in PENPOT_TOOLS]}

    selected = _round(
        relevant_tools,
        PENPOT_SCHEMAS,
        disabled_tools={"mcp__penpot__execute_code"},
        mcp_gated_names=mgr.gated_tool_names(),
    )

    names = _names(selected)
    assert "mcp__penpot__execute_code" not in names
    assert "mcp__penpot__get_page" in names


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


# ── 3. Stream interruption does not lose pending tool availability ────────


def test_tool_availability_is_stable_across_rounds_regardless_of_retry_state():
    # The agent loop recomputes the schema list every round (see
    # stream_agent_loop's two _tool_schemas_for_round call sites). Binding
    # must not depend on how many rounds already ran, whether a previous
    # round's stream was interrupted/retried, or whether the missing-tool
    # self-unblock ever fired -- it was a safety net for the old bug, not a
    # requirement for correct behavior.
    mgr = McpManager()
    mgr._tools = {"penpot": [{"name": n, "description": n, "input_schema": {}} for n in PENPOT_TOOLS]}
    gated = mgr.gated_tool_names()

    # Round 1: narrow relevant_tools, as if this is the very first round.
    round_1 = _names(_round({"ask_user"}, PENPOT_SCHEMAS, mcp_gated_names=gated))
    # Round 2: a DIFFERENT (still narrow, still missing penpot) relevant_tools
    # set, as if a mid-stream interruption forced a retry that recomputed
    # retrieval independently and still didn't retrieve the right thing.
    round_2 = _names(_round({"manage_memory"}, PENPOT_SCHEMAS, mcp_gated_names=gated))

    expected = {f"mcp__penpot__{n}" for n in PENPOT_TOOLS}
    assert expected <= round_1
    assert expected <= round_2  # not lost on the second round either


def test_mcp_mgr_none_yields_no_mcp_schemas_without_raising():
    # A dropped/None mcp_mgr (e.g. plan-mode disable, public endpoint scoping)
    # must degrade to "no MCP tools this turn", never a crash that would cut
    # the stream short.
    selected = _round({"ask_user"}, [], mcp_gated_names=None)
    assert _mcp_names(selected) == set()


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

import inspect

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


def _schemas(mgr: McpManager):
    return mgr.get_all_openai_schemas()


def test_oversized_server_is_demoted_while_its_small_peers_stay_bound():
    # The exact 2026-09-16 inventory: firecrawl (27) alongside four small
    # purpose-built servers. Only firecrawl loses the always-bound guarantee.
    mgr = _mgr(
        ntfy=("ntfy", 2),
        firecrawl=("firecrawl", 27),
        seqthink=("sequentialthinking", 1),
        context7=("context7", 2),
        penpot=("penpot", 5),
    )
    assert [row[0] for row in mgr.demoted_servers()] == ["firecrawl"]

    selected = _round({"ask_user"}, _schemas(mgr), mcp_gated_names=mgr.gated_tool_names())
    sent = _mcp_names(selected)

    assert not any(n.startswith("mcp__firecrawl__") for n in sent)
    # ...and the whole point of the original fix survives: a vague follow-up
    # still gets every small connected server.
    assert len(sent) == 10
    for prefix in ("mcp__ntfy__", "mcp__seqthink__", "mcp__context7__", "mcp__penpot__"):
        assert any(n.startswith(prefix) for n in sent), prefix


def test_per_server_cap_boundary_is_inclusive():
    # "At the cap" is still a handful; one over is not. Pinned so a later
    # off-by-one cannot silently change which real servers stay bound.
    cap = mm._MCP_ALWAYS_BOUND_SERVER_MAX_TOOLS
    assert _mgr(srv=("srv", cap)).demoted_servers() == []
    assert [row[0] for row in _mgr(srv=("srv", cap + 1)).demoted_servers()] == ["srv"]


def test_many_small_servers_are_trimmed_largest_first_to_the_total_cap():
    # Death by a thousand cuts: every server is individually under the
    # per-server cap, but together they blow the total budget. Demotion takes
    # the biggest first, so the fewest servers lose the guarantee.
    mgr = _mgr(
        big=("big", 8),
        mid=("mid", 7),
        small=("small", 6),
        tiny=("tiny", 5),
    )  # 26 tools, total cap is 24
    demoted = [row[0] for row in mgr.demoted_servers()]

    assert demoted == ["big"]  # one demotion is enough; not two, not the small ones
    sent = _mcp_names(_round({"ask_user"}, _schemas(mgr), mcp_gated_names=mgr.gated_tool_names()))
    assert len(sent) == 18
    assert not any(n.startswith("mcp__big__") for n in sent)


def test_demotion_order_is_stable_for_equally_sized_servers():
    # The schema list is part of the cached prompt prefix; a set-iteration
    # tie-break would reshuffle it between rounds and invalidate that cache.
    mgr = _mgr(bbb=("bbb", 7), aaa=("aaa", 7), ccc=("ccc", 7), ddd=("ddd", 7))
    first = [row[0] for row in mgr.demoted_servers()]
    assert first == [row[0] for row in mgr.demoted_servers()]
    assert first == ["aaa"]  # 28 tools -> drop one; alphabetical tie-break


def test_demoted_tool_is_still_reachable_once_retrieval_surfaces_it():
    # Demotion is gating, not hiding: exactly like a builtin catalog tool, a
    # real RAG/intent hit (or the missing-tool re-arm, which widens the same
    # relevant_tools set) puts the demoted tool back in the payload.
    mgr = _mgr(firecrawl=("firecrawl", 27))
    wanted = "mcp__firecrawl__firecrawl_t3"

    selected = _round(
        {"ask_user", wanted}, _schemas(mgr), mcp_gated_names=mgr.gated_tool_names()
    )
    assert _mcp_names(selected) == {wanted}  # the one that was asked for, not all 27


def test_demoted_server_stays_listed_in_the_prompt_with_a_way_back():
    # If a demoted server went invisible, this would be the vanishing bug in a
    # new form. The prompt keeps the full per-tool listing (which is also what
    # ToolIndex.index_mcp_tools parses, so the tools stay retrievable) and adds
    # one line saying the schemas are not attached and how to ask for one.
    mgr = _mgr(firecrawl=("firecrawl", 27))
    text = mgr.get_tool_descriptions_for_prompt()

    assert text.count("mcp__firecrawl__") == 27  # every tool still discoverable
    assert "CONNECTED AND WORKING" in text
    assert "not attached this turn" in text
    # Wording must stay compatible with the missing-tool self-unblock that
    # re-arms the tool, or the "way back" is a dead end.
    assert al._claims_missing_tools("I do not have the mcp__firecrawl__scrape tool available.")


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


def test_agent_debug_line_names_the_demoted_servers():
    # An operator reading a log tail must be able to tell "demoted for size"
    # apart from disconnected / disabled / a routing bug, without a code read.
    src = inspect.getsource(al.stream_agent_loop)
    assert "mcp_demoted=%s" in src
    assert "demoted_servers(_mcp_disabled_map)" in src


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


def test_the_note_speaks_the_phrase_the_self_unblock_listens_for():
    """The note tells the model how to ask for a schema. If its wording and the
    detector drift apart, the instruction becomes a dead end."""
    from src.agent_loop import _claims_missing_tools

    assert _claims_missing_tools(
        "I do not have the mcp__builtin_browser__browser_click tool available."
    )
