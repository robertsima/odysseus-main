"""Missing-tool self-unblock widens to the tools the claim points at first.

Observed (2026-09-10 logs): one "I don't have a GitHub publishing tool" round
re-armed 90 tools, the next round carried 118 schemas (14,180 schema tokens)
and every cached prefix was lost. The claim named what it needed; widening
should too, and only fall back to the whole registry when nothing specific
can be identified.
"""
from src.agent_loop import _claims_missing_tools, _name_list, _targeted_rearm_tools

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
