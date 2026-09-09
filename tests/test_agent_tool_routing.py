"""Retrieval must surface the worktree tool for source-control requests.

Observed in production: a "commit this and open a PR" turn selected 24 tools and
manage_agent_worktree was not among them, so the model used bash for git work and
the push failed. Keyword hints make the routing deterministic rather than leaving
it to embedding similarity.
"""

import re

import pytest

from src.tool_index import ALWAYS_AVAILABLE, BUILTIN_TOOL_DESCRIPTIONS, ToolIndex

pytestmark = pytest.mark.area_unit


def keyword_tools(query: str) -> set:
    """Mirror of the keyword pass in ToolIndex.get_tools_for_query."""
    ql = query.lower()
    out = set()
    for keywords, tools in ToolIndex._KEYWORD_HINTS.items():
        if any(re.search(rf"\b{re.escape(kw)}\b", ql) for kw in keywords):
            out |= set(tools)
    return out


@pytest.mark.parametrize(
    "query",
    [
        "push this branch",
        "open a PR for these changes",
        "commit and publish my changes",
        "raise a pr against dev",
        "make a draft pr",
        "fix this bug in the repo and open a pull request",
        "create a branch and commit the fix",
        "apply this patch to the codebase",
    ],
)
def test_source_control_requests_reach_the_worktree_tool(query):
    assert "manage_agent_worktree" in keyword_tools(query)


@pytest.mark.parametrize(
    "query",
    [
        "check the app logs",
        "what went wrong last night",
        "show me the stack trace",
        "look at the server logs for errors",
    ],
)
def test_debugging_requests_reach_the_log_reader(query):
    assert "read_app_logs" in keyword_tools(query)


@pytest.mark.parametrize(
    "query",
    [
        "what is the weather today",
        "send an email to bob",
        "what is on my calendar tomorrow",
        "summarize this article",
    ],
)
def test_unrelated_requests_do_not_pull_in_the_worktree_tool(query):
    selected = keyword_tools(query)
    assert "manage_agent_worktree" not in selected
    assert "read_app_logs" not in selected


@pytest.mark.parametrize("tool", ["manage_agent_worktree", "read_app_logs"])
def test_the_tools_are_indexed_for_retrieval(tool):
    """Absent from the index, retrieval can never select them at all."""
    assert tool in BUILTIN_TOOL_DESCRIPTIONS
    assert BUILTIN_TOOL_DESCRIPTIONS[tool].strip()


def test_the_worktree_description_warns_against_git_in_bash():
    """The description is what the model reads when choosing; it has to say this."""
    text = BUILTIN_TOOL_DESCRIPTIONS["manage_agent_worktree"].lower()
    assert "bash" in text
    assert "credential" in text or "authenticate" in text


def test_neither_tool_is_always_available():
    """They are admin-only and situational; they must not ride along on every turn."""
    assert "manage_agent_worktree" not in ALWAYS_AVAILABLE
    assert "read_app_logs" not in ALWAYS_AVAILABLE
