"""Re-port: a mixed request must not have its own tools cleared.

Repro, from the 2026-09-19 logs of the scheduled "Applied Job Status Tracker"
task (and originally from a 2026-08-13 chat):

    "Search my inbox and audit my inbox for job applications from the last 4
     months ... update the existing Rolling Report within AI Mind Vault"

The run ended with "Blocked before I could perform the audit: the email-audit
tool/schema is not available in this run". Two mechanisms conspired:

1. `_LOCAL_COMPUTER_REFERENCE_RE` matched `on|from <any bare word>`, so
   "confirmations from Gmail" read as work targeted at a machine named
   "Gmail".
2. The Terminus clamp then REPLACED the whole selection with the shell/file
   toolset, discarding every email tool domain detection had just seeded --
   and the system prompt it installs tells the model not to use email tools.

Both were fixed in the fork (`f8882905`, `3e2a0eb5`, `5181684e`) and lost in
the 2026-09-18 upstream sync; this re-ports them. The wider fork machinery in
tests/test_agent_self_blocking_toolsets.py (missing-tool re-arm,
starved-domain repair) is still on the backlog and stays skipped.
"""

import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from src.agent_loop import (  # noqa: E402
    _DOMAIN_TOOL_MAP,
    _WORKSPACE_TERMINUS_TOOLS,
    _classify_agent_request,
    _looks_like_local_computer_request,
    apply_terminus_toolset,
)

REPRO = (
    "Search my inbox and audit my inbox for job applications from the last 4 "
    "months. maintain an overall count of jobs applied to, rejections, "
    "ghosted, and update the existing Rolling Report within AI Mind Vault"
)


# ── "on/from <word>" must mean a machine, not any proper noun ───────────────

@pytest.mark.parametrize("text", [
    "serve qwen on gpu-box",
    "check disk usage on pi4",
    "tail the output from gpu-box-2",
    "copy the logs from 10.0.0.7",
    "download the model on zimaos",
    "list what's running on my machine",
    "clean up the local files",
])
def test_named_machines_still_switch_to_terminus(text):
    assert _looks_like_local_computer_request(text), text


@pytest.mark.parametrize("text", [
    "roll up job application confirmations from Gmail",
    "audit my inbox for rejections from recruiters this month",
    "job applications from LinkedIn",
    "count the rejections from Amazon",
    "summarize the email from Sarah",
    "the article from Reuters",
    "on Monday I applied to three jobs",
    "job applications from the last 4 months",
    "summarise emails from yesterday",
])
def test_ordinary_proper_nouns_do_not_switch_to_terminus(text):
    """`on|from <any bare word>` used to match, so an inbox question about
    applications "from Gmail" was read as machine-targeted work and swapped
    the whole toolset for shell/file tools."""
    assert not _looks_like_local_computer_request(text), text


# ── Terminus must merge, not replace, on a mixed request ────────────────────

def test_the_repro_really_is_multi_domain():
    domains = _classify_agent_request([{"role": "user", "content": REPRO}], REPRO).get("domains") or set()
    assert "email" in domains, domains


def test_a_mixed_request_keeps_its_email_tools():
    """The failure this whole file exists for."""
    selected = set(_DOMAIN_TOOL_MAP["email"]) | {"read_file", "write_file"}
    out = apply_terminus_toolset(selected, query_matched=selected, domains={"email", "files"})
    assert "audit_emails" in out
    assert _DOMAIN_TOOL_MAP["email"] <= out
    assert _WORKSPACE_TERMINUS_TOOLS <= out


@pytest.mark.parametrize("domain", sorted({
    "email", "documents", "notes_calendar_tasks", "contacts", "sessions",
    "cookbook", "integrations",
}))
def test_every_assistant_domain_survives_the_clamp(domain):
    selected = set(_DOMAIN_TOOL_MAP[domain])
    out = apply_terminus_toolset(selected, query_matched=selected, domains={domain, "files"})
    assert selected <= out, sorted(selected - out)


def test_a_pure_coding_request_still_swaps_to_terminus_only():
    """Replacement is right when nothing else was detected — that is what keeps
    a plain "fix the failing test" turn focused."""
    out = apply_terminus_toolset(
        {"manage_notes", "manage_calendar"}, query_matched=set(), domains={"files"},
    )
    assert out == set(_WORKSPACE_TERMINUS_TOOLS)


def test_retrieval_matches_ride_across_the_swap():
    """A tool that scored against the user's own words is evidence of intent,
    even when no built-in domain claims it."""
    out = apply_terminus_toolset(
        {"trigger_research", "manage_notes"},
        query_matched={"trigger_research"},
        domains=set(),
    )
    assert "trigger_research" in out
    assert "manage_notes" not in out


def test_mcp_tools_survive_the_swap_with_no_domain():
    """A user-added MCP server has no domain to protect it: the intent
    classifier has never heard of a server installed five minutes ago."""
    out = apply_terminus_toolset(
        {"mcp__51452cf0__ntfy_me", "manage_notes"}, query_matched=set(), domains=set(),
    )
    assert "mcp__51452cf0__ntfy_me" in out


def test_the_clamp_never_drops_terminus_tools_themselves():
    out = apply_terminus_toolset(set(), query_matched=set(), domains={"email"})
    assert _WORKSPACE_TERMINUS_TOOLS <= out


# ── the one email tool deterministic seeding could not supply ───────────────

def test_audit_emails_is_part_of_the_email_domain():
    """It is the whole-mailbox report tool the Rolling Report task needs. It
    was missing from the domain map, so an email turn whose vector retrieval
    missed it had no deterministic path to it at all."""
    assert "audit_emails" in _DOMAIN_TOOL_MAP["email"]


# ── a scheduled task's deliberately composed toolset must survive ───────────

def test_a_scheduled_tasks_composed_toolset_is_never_discarded():
    """The scheduler composes relevant_tools itself (RAG + the assistant's
    always-available set + shell defaults) and hands it to the loop. Nothing in
    the loop's own routing should then throw that away: for a caller-provided
    set, `_pre_domain_tools` IS the caller's set, so every tool rides across
    even on the swap path. This is the "Applied Job Status Tracker" failure —
    the task was composed WITH audit_emails and ran without it."""
    from src.task_scheduler import compose_task_relevant_tools
    from src.tool_index import ASSISTANT_ALWAYS_AVAILABLE

    composed = compose_task_relevant_tools(
        ["list_emails", "read_email"], ASSISTANT_ALWAYS_AVAILABLE, set(),
    )
    assert "audit_emails" in composed

    # The loop's clamp fires on a prompt that mentions a machine-ish token.
    survived = apply_terminus_toolset(
        composed, query_matched=composed, domains=set(),
    )
    assert composed <= survived, sorted(composed - survived)


def test_a_crew_allowlist_still_removes_what_it_disables():
    """Merging must not resurrect a tool the task's crew switched off."""
    from src.task_scheduler import compose_task_relevant_tools
    from src.tool_index import ASSISTANT_ALWAYS_AVAILABLE

    composed = compose_task_relevant_tools(
        [], ASSISTANT_ALWAYS_AVAILABLE, {"audit_emails"},
    )
    assert "audit_emails" not in composed
