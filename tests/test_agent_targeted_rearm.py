"""Missing-tool self-unblock widens to the tools the claim points at first.

Observed (2026-09-10 logs): one "I don't have a GitHub publishing tool" round
re-armed 90 tools, the next round carried 118 schemas (14,180 schema tokens)
and every cached prefix was lost. The claim named what it needed; widening
should too.

Follow-up (2026-09-16): the whole-registry fallback underneath it is gone —
see the last section. That makes the targeted path the path that has to work,
so the sections below pin the two ways it was silently failing: the app's own
prose->tool table was never consulted, and phrase matching broke on a hyphen.
"""
from src.agent_loop import (
    _claims_missing_tools,
    _flatten_capability_phrase,
    _name_list,
    _rearm_domain_closure,
    _targeted_rearm_tools,
)

POOL = {
    "manage_agent_worktree", "read_file", "write_file", "bash", "web_search", "web_fetch",
    "manage_calendar", "send_email", "list_emails", "delegate_to_claude_code", "manage_mcp",
    "generate_image", "ask_user",
}


class _Index:
    def __init__(self, hits):
        self.hits = hits
        self.queries = []

    def retrieve(self, query, k=8):
        self.queries.append((query, k))
        return self.hits


def test_named_tool_is_targeted():
    text = "I don't have the `manage_calendar` tool available in this turn."
    assert _claims_missing_tools(text)
    assert _targeted_rearm_tools(text, POOL) == {"manage_calendar"}


def test_keyword_intent_targets_the_worktree_tool_for_push_claims():
    text = ("The current MCP registry shows only ntfy. No GitHub publishing tool is "
            "available, so I cannot push the branch or open a pull request.")
    found = _targeted_rearm_tools(text, POOL)
    assert "manage_agent_worktree" in found
    assert "send_email" not in found


def test_index_neighbours_are_included_but_clipped_to_the_pool():
    idx = _Index(["web_search", "not_a_real_tool"])
    found = _targeted_rearm_tools("search tools are unavailable in this turn", POOL, tool_idx=idx)
    assert "web_search" in found
    assert "not_a_real_tool" not in found
    assert idx.queries and idx.queries[0][1] == 12


def test_nothing_specific_means_empty_so_the_caller_widens_fully():
    assert _targeted_rearm_tools("Tools are not available right now.", POOL) == set()
    assert _targeted_rearm_tools("", POOL) == set()
    assert _targeted_rearm_tools("push it", set()) == set()


def test_short_names_do_not_match_inside_words():
    # "bash" must not fire on "bashful"; "ls"-length names are ignored entirely.
    assert "bash" not in _targeted_rearm_tools("I feel bashful about tools not available", POOL)


# ── The re-arm log has to agree with itself ─────────────────────────────
#
# The line was `"re-armed %d tool(s) %s" % (len(_rearm_new), sorted(_rearm_new)[:25])`,
# which printed `re-armed 33 tool(s) [...25 names...]` with no marker. It is the
# one line an operator greps to answer "was the tool I needed re-armed?", and
# the 8 dropped names are exactly the ones being looked for — so it answered
# that question confidently and wrongly. Same clip, at 20, in the directive the
# MODEL reads.


def test_a_clipped_list_says_that_it_was_clipped():
    rendered = _name_list({f"tool_{i:02d}" for i in range(33)}, 25)
    assert rendered.count(",") == 24, "still shows 25 names"
    assert "+8 more" in rendered, f"the other 8 vanished without a trace: {rendered}"


def test_a_short_list_gains_no_marker():
    assert _name_list({"bash", "read_file"}, 25) == "bash, read_file"


def test_an_exactly_full_list_gains_no_marker():
    assert "more" not in _name_list({f"t{i}" for i in range(25)}, 25)


def test_an_empty_set_is_stated_not_rendered_blank():
    assert _name_list(set(), 25) == "(none)"


# ── The 2026-09-16 miss: the table that knew the answer was not asked ──────
#
# The round below ended the turn with zero tool calls. `_missing_tool_signal`
# caught it (structurally — two of the three bullets never say "tool"), the
# targeted pass then returned the git/file tools for bullets 2 and 3 and
# nothing at all for bullet 1, and `read_app_logs` — the one tool the user's
# actual request needed — rode into the round after on a full-registry re-arm
# alongside ~80 strangers.
#
# Two separate faults, both of them in the matching and neither in the tables:
#
#   * `_SKILL_TOOLSET_ALIASES` has carried `"application-log access" ->
#     read_app_logs` the whole time. It is the app's own prose->tool
#     vocabulary and it is exactly what a refusal speaks, but it was wired
#     only into skill front matter, never into the re-arm.
#   * `ToolIndex._KEYWORD_HINTS` has carried `"application log"`. The round
#     wrote "application-log", and `\bapplication log\b` does not match across
#     a hyphen.
#
# Either one alone would have armed the tool. Both missed, so the fallback
# fired, which is why "the targeted path missed" and "the fallback dumped
# everything" were the same bug wearing two hats.

INCIDENT_REFUSAL = (
    "The issue is a capability mismatch in this session:\n"
    "- I do not have application-log access.\n"
    "- I do not have repository or filesystem access.\n"
    "- I do not have the agent-launcher, Git, or push tools."
)

LOG_POOL = POOL | {"read_app_logs", "manage_memory", "manage_skills", "grep", "edit_file"}


def test_the_incident_round_now_arms_the_tool_its_request_needed():
    assert _claims_missing_tools(INCIDENT_REFUSAL)
    found = _targeted_rearm_tools(INCIDENT_REFUSAL, LOG_POOL)
    assert "read_app_logs" in found
    # ...and it is still a handful, not the registry: the other bullets
    # legitimately name source-control and file work.
    assert len(found) <= 12, sorted(found)


def test_a_hyphenated_compound_resolves_like_the_spaced_spelling():
    spaced = _targeted_rearm_tools("I do not have application log access.", LOG_POOL)
    hyphened = _targeted_rearm_tools("I do not have application-log access.", LOG_POOL)
    assert "read_app_logs" in spaced
    assert hyphened == spaced, "a hyphen must not change which tools are named"


def test_the_skill_alias_table_is_the_refusal_vocabulary():
    # Each of these is a verbatim key in _SKILL_TOOLSET_ALIASES and resolved
    # to nothing at all through tool names or keyword hints alone.
    assert "manage_memory" in _targeted_rearm_tools(
        "I do not have memory management available.", LOG_POOL)
    assert "manage_skills" in _targeted_rearm_tools(
        "I have no skill-management capability here.", LOG_POOL)
    assert {"bash", "read_app_logs"} <= _targeted_rearm_tools(
        "I lack shell access and application-log access.", LOG_POOL)


def test_unavailable_delegation_capability_rearms_its_matching_toolset():
    pool = LOG_POOL | {"delegate_to_agent", "delegate_to_claude_code", "manage_agent_loadout"}
    found = _targeted_rearm_tools(
        "The agent delegation capability is unavailable to me in this session.", pool,
    )
    assert {"delegate_to_agent", "manage_agent_loadout"} <= found


def test_session_scoped_calendar_access_refusal_rearms_calendar():
    assert "manage_calendar" in _targeted_rearm_tools(
        "calendar access isn't available in this session", POOL,
    )


def test_flattening_leaves_tool_names_and_ordinary_text_alone():
    assert _flatten_capability_phrase("Application-Log  Access") == "application log access"
    assert _flatten_capability_phrase("well-being") == "well being"
    # The raw-text pass is what matches underscored tool names, so a name is
    # still found even though flattening would have split it into words.
    assert "read_app_logs" in _targeted_rearm_tools(
        "The read_app_logs tool was not given to me.", LOG_POOL)


# ── The bounded last resort ────────────────────────────────────────────────
#
# `_rearm_domain_closure` replaced `_rearm_pool - _relevant_tools`. It widens
# by _DOMAIN_TOOL_MAP group or by MCP server, and only for groups this turn
# has evidence for, so the scope it reports is a bound and not a synonym for
# "everything".

MCP_POOL = LOG_POOL | {
    "mcp__firecrawl__scrape", "mcp__firecrawl__crawl", "mcp__penpot__execute_code",
}


def test_a_targeted_hit_pulls_in_its_own_group_as_prerequisites():
    # A round handed `apply_patch` alone blocks again on `read_file`; the
    # prompt's rule packs are keyed on the same groups, so a half-armed
    # domain is a half-explained one.
    tools, labels = _rearm_domain_closure({"read_file"}, "", MCP_POOL)
    assert labels == ["files"]
    assert {"read_file", "write_file", "bash"} <= tools


def test_a_named_mcp_server_is_a_group_of_its_own():
    tools, labels = _rearm_domain_closure(
        set(), "I do not have the firecrawl tools attached this turn.", MCP_POOL)
    assert labels == ["mcp:firecrawl"]
    assert tools == {"mcp__firecrawl__scrape", "mcp__firecrawl__crawl"}
    assert "mcp__penpot__execute_code" not in tools


def test_a_domain_retrieval_half_selected_is_completed():
    # The honest reading of "I can't do it with these": the turn IS about
    # files, the selection just stopped short.
    tools, labels = _rearm_domain_closure(
        set(), "I cannot do that here.", MCP_POOL, already_selected={"read_file", "grep"})
    assert labels == ["files"]
    assert "write_file" in tools


def test_ambient_tools_are_not_evidence_about_this_turn():
    # ALWAYS_AVAILABLE rides on every turn, so counting it would put the
    # documents domain into the scope of every domain re-arm in the app.
    from src.tool_index import ALWAYS_AVAILABLE

    _, labels = _rearm_domain_closure(set(), "I cannot do that.", MCP_POOL,
                                      already_selected=set(ALWAYS_AVAILABLE))
    assert labels == []


def test_a_claim_with_no_evidence_widens_to_nothing_at_all():
    # This is the case the full re-arm existed for, and the case it was worst
    # at: 142 schemas do not conjure a capability the host does not have.
    tools, labels = _rearm_domain_closure(
        set(), "I do not have quantum teleportation.", MCP_POOL)
    assert (tools, labels) == (set(), [])


# ── The fallback is gone, and the log line still says how far it went ──────


def test_the_loop_no_longer_re_arms_the_whole_registry():
    import inspect

    from src import agent_loop

    src = inspect.getsource(agent_loop.stream_agent_loop)
    assert "_rearm_pool - _relevant_tools" not in src, (
        "the full-registry fallback is back: ~12k schema tokens per remaining "
        "round, and a schema list made mostly of strangers"
    )
    assert "_rearm_domain_closure(" in src


def test_the_self_unblock_line_still_reports_scope_and_signal():
    import inspect

    from src import agent_loop

    src = inspect.getsource(agent_loop.stream_agent_loop)
    assert "missing-tool self-unblock on round %d (%s, via %s)" in src
    # The scope is what makes the remaining widening auditable, so it has to
    # carry the groups rather than a bare word.
    assert '"domain:" + (",".join(_domain_labels)' in src
    assert 'if _rearm_scope.startswith("domain"):' in src, (
        "only the bounded widening may spend the one _MAX_TOOLSET_REARMS "
        "budget; a targeted hit stays free"
    )
