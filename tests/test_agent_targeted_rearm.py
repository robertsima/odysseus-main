"""Missing-tool self-unblock widens to the tools the claim points at first.

Observed (2026-09-10 logs): one "I don't have a GitHub publishing tool" round
re-armed 90 tools, the next round carried 118 schemas (14,180 schema tokens)
and every cached prefix was lost. The claim named what it needed; widening
should too, and only fall back to the whole registry when nothing specific
can be identified.
"""
from src.agent_loop import _claims_missing_tools, _targeted_rearm_tools

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
