"""Regression: mixed-domain requests had their tools cleared, so the agent
blocked itself.

Repro from a real session (2026-08-13):

    "Search my inbox and audit my inbox for job applications from the last 4
     months. maintain an overall count of jobs applied to, rejections,
     ghosted, and update the existing Rolling Report within AI Mind Vault"

The agent replied "I don't have callable inbox/email search tools or AI Mind
Vault document tools available in this turn" and did nothing. Three separate
mechanisms each removed tools the request needed:

1. `chat_routes` — `_explicit_web_intent` fires on the bare word "search", and
   its lockdown stripped read_file/write_file/edit_file/create_document/
   send_email/manage_notes/bash/python. Searching your own inbox is not a web
   lookup.
2. `agent_loop` — the Odysseus Terminus clamp REPLACED the whole selection with
   the file/shell toolset, discarding every email and document tool that domain
   detection had just seeded.
3. Nothing recovered afterwards: the model wrote its excuse and the turn ended.

The fixes are, respectively: gate the web lockdown on there being no personal
domain in the message, merge (not replace) the Terminus toolset when another
domain was detected, and re-arm the full permitted toolset once when a round
ends by claiming a tool was missing.
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

from src.agent_loop import (
    _DOMAIN_TOOL_MAP,
    _STARVED_DOMAIN_LABELS,
    _WORKSPACE_TERMINUS_TOOLS,
    _claims_missing_tools,
    _classify_agent_request,
    _looks_like_local_computer_request,
)


REPRO = (
    "Search my inbox and audit my inbox for job applications from the last 4 "
    "months. maintain an overall count of jobs applied to, rejections, ghosted, "
    "and update the existing Rolling Report within AI Mind Vault"
)

PERSONAL_DOMAINS = {"email", "documents", "notes_calendar_tasks",
                    "contacts", "sessions", "files"}


def _domains(text):
    return _classify_agent_request([{"role": "user", "content": text}], text)["domains"]


# ── 1. the request really is multi-domain ────────────────────────────────────

def test_repro_detects_both_email_and_file_domains():
    """The clamp only misbehaves because both domains are genuinely present."""
    domains = _domains(REPRO)
    assert "email" in domains
    assert "files" in domains


# ── 2. Terminus must merge, not replace, on a mixed request ──────────────────

def _apply_terminus_clamp(selected, domains):
    """Mirror of the clamp in stream_agent_loop, exercised directly.

    The clamp lives inside a 700-line async generator that needs a live
    endpoint, a session, and an MCP manager to reach; the decision it makes is
    this one line and is worth pinning on its own.
    """
    other = set(domains) & {
        "email", "documents", "notes_calendar_tasks",
        "contacts", "sessions", "cookbook", "integrations",
    }
    if other:
        return set(selected) | set(_WORKSPACE_TERMINUS_TOOLS)
    return set(_WORKSPACE_TERMINUS_TOOLS)


def test_mixed_request_keeps_email_tools_after_terminus_clamp():
    domains = _domains(REPRO)
    selected = set()
    for d in domains:
        selected |= _DOMAIN_TOOL_MAP.get(d, set())
    result = _apply_terminus_clamp(selected, domains)

    assert {"list_emails", "read_email", "audit_emails"} <= result, (
        "the email tools the request asked for were cleared again"
    )
    assert _WORKSPACE_TERMINUS_TOOLS <= result, "file/shell tools must still arrive"


def test_pure_coding_request_still_swaps_to_terminus_only():
    """Merging must not leak assistant tools into a plain repo task."""
    text = "fix the failing test in the api route on this machine"
    domains = _domains(text)
    assert not (domains & {"email", "documents", "notes_calendar_tasks"})
    result = _apply_terminus_clamp(_DOMAIN_TOOL_MAP["files"], domains)
    assert result == set(_WORKSPACE_TERMINUS_TOOLS)


# ── 3. the web-intent lockdown must spare personal-domain searches ───────────

@pytest.mark.parametrize("text", [
    REPRO,
    "search my inbox for the interview reply",
    "look up Chris's phone number",
    "search my notes for the meeting agenda",
    "search my vault for the rolling report",
    "search my chats for what we decided about the deploy",
])
def test_personal_searches_are_not_treated_as_pure_web_lookups(text):
    """These all trip `_explicit_web_intent` ("search"/"look up"), so the
    lockdown must be gated on the personal domain also being present."""
    assert _domains(text) & PERSONAL_DOMAINS, text


@pytest.mark.parametrize("text", [
    "what is the weather today",
    "look up the latest news on the fed rate",
    "google the current exchange rate",
])
def test_real_web_lookups_still_get_locked_down(text):
    """Guard the other direction: a genuine web lookup must stay narrow."""
    assert not (_domains(text) & PERSONAL_DOMAINS), text


# ── 4. missing-tool claims are detected so the loop can re-arm ───────────────

@pytest.mark.parametrize("text", [
    "I'm still blocked: I don't have callable inbox/email search tools or AI Mind "
    "Vault document tools available in this turn, so I can't search your inbox.",
    "I don't have the tools to do that.",
    "I do not have any email tools available.",
    "There are no document tools in my list.",
    "No email tools are available to me right now.",
    "The email tools aren't available this turn.",
    "I wasn't given the file tools I need.",
    "I lack the tools required to update the report.",
    "I don't have access to your inbox.",
])
def test_missing_tool_excuses_are_detected(text):
    assert _claims_missing_tools(text), text


@pytest.mark.parametrize("text", [
    "Still blocked: Gmail is returning Too many simultaneous connections again.",
    "I read 40 emails and found 12 applications, 5 rejections, and 3 ghosts.",
    "The tools are available, but the mailbox is empty.",
    "I used the audit_emails tool to summarize the inbox.",
    "Let me know if you want a different date range.",
    "",
])
def test_real_failures_and_normal_answers_do_not_trigger_a_rearm(text):
    """A genuine upstream error is not self-blocking — the tool ran and failed.
    Re-arming there would burn a round for nothing."""
    assert not _claims_missing_tools(text), text


# ── 5. "on/from <word>" must mean a machine, not any proper noun ─────────────

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
    "job applications from LinkedIn",
    "count the rejections from Amazon",
    "summarize the email from Sarah",
    "the article from Reuters",
    "on Monday I applied to three jobs",
    "job applications from the last 4 months",
])
def test_ordinary_proper_nouns_do_not_switch_to_terminus(text):
    """`on|from <any bare word>` used to match, so an inbox question about
    applications "from LinkedIn" was read as machine-targeted work and swapped
    the whole toolset for shell/file tools."""
    assert not _looks_like_local_computer_request(text), text


# ── 6. named integrations must not fall through to low-signal ────────────────

@pytest.mark.parametrize("text", [
    "add milk to my todoist",
    "put this on my todoist list",
    "check my todoist",
    "sync todoist",
])
def test_todoist_requests_get_the_tasks_domain(text):
    """"todoist" is not "todo" + a word boundary, so these matched no domain,
    were flagged low-signal, and relied entirely on embedding retrieval to
    surface the Todoist MCP tools."""
    intent = _classify_agent_request([{"role": "user", "content": text}], text)
    assert "notes_calendar_tasks" in intent["domains"], text
    assert intent["low_signal"] is False, text


# ── 7. the starved-domain note can name every domain ─────────────────────────

def test_every_domain_has_a_human_readable_label():
    """The note is shown to the model, which repeats it to the user — an
    unlabelled domain would surface a raw internal key like
    'notes_calendar_tasks'."""
    assert set(_DOMAIN_TOOL_MAP) <= set(_STARVED_DOMAIN_LABELS)
