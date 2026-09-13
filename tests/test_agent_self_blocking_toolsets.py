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
    _explicitly_named_skills,
    _is_explicit_continuation,
    _looks_like_local_computer_request,
    _looks_like_research_request,
    _looks_like_vault_request,
    _looks_like_workspace_coding_request,
    _retained_tools_for_turn,
    _tools_used_in_conversation,
    apply_terminus_toolset,
    repair_starved_domains,
)


def test_generic_tools_are_not_retained_for_non_shell_followup():
    retained, suppressed = _retained_tools_for_turn(
        {"bash", "read_file", "audit_emails"},
        query="continue the inbox audit",
        domains={"email"},
        workspace=None,
    )
    assert retained == {"read_file", "audit_emails"}
    assert suppressed == {"bash"}


def test_generic_tools_remain_available_for_workspace_followup():
    retained, suppressed = _retained_tools_for_turn(
        {"bash", "read_file"},
        query="run the tests in the workspace",
        domains={"workspace"},
        workspace="/app/data/development/odysseus-main",
    )
    assert retained == {"bash", "read_file"}
    assert suppressed == set()


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

def _apply_terminus_clamp(selected, domains, query_matched=None):
    """Call the real clamp.

    It used to be mirrored here, because the decision lived inside a 700-line
    async generator that needs a live endpoint, a session and an MCP manager to
    reach. It is now `apply_terminus_toolset`, so these tests pin the shipping
    code instead of a copy of it that could drift.

    `query_matched` defaults to everything selected: in the loop it is what
    retrieval returned for this query, and the callers below are modelling
    exactly that.
    """
    selected = set(selected or set())
    return apply_terminus_toolset(
        selected,
        query_matched=selected if query_matched is None else query_matched,
        domains=domains,
    )


def test_mcp_tools_survive_the_terminus_swap_with_no_domain():
    """A user-added MCP server has no domain, so it needs its own guard.

    "send a test notification" classifies as domains=[] -- the intent
    classifier has no keywords for a server the user installed five minutes
    ago -- but tool-RAG retrieved the ntfy tools correctly. The swap used to
    discard them and the agent reported the tool wasn't callable.
    """
    text = "send a test notification"
    domains = _domains(text)
    assert not (domains & {
        "email", "documents", "notes_calendar_tasks",
        "contacts", "sessions", "cookbook", "integrations",
    }), "precondition: no built-in domain protects this request"

    selected = {"mcp__51452cf0__ntfy_me", "mcp__51452cf0__ntfy_me_fetch", "list_emails"}
    # Only the MCP tools were matched for this query; list_emails came from a
    # domain seed, so it is not evidence of what the user asked for.
    result = _apply_terminus_clamp(
        selected, domains,
        query_matched={"mcp__51452cf0__ntfy_me", "mcp__51452cf0__ntfy_me_fetch"},
    )

    assert {"mcp__51452cf0__ntfy_me", "mcp__51452cf0__ntfy_me_fetch"} <= result, (
        "retrieval matched the MCP tools for this query and the swap dropped them"
    )
    assert _WORKSPACE_TERMINUS_TOOLS <= result, "file/shell tools must still arrive"
    assert "list_emails" not in result, (
        "a tool nothing about this query matched still swaps out"
    )


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
    result = _apply_terminus_clamp(
        _DOMAIN_TOOL_MAP["files"], domains, query_matched=set(),
    )
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


def test_curly_apostrophe_missing_tool_excuse_is_detected():
    text = "I don\u2019t have a local-pi-delegation or repo-editing tool available in this turn."
    assert _claims_missing_tools(text)


def test_exact_registered_skill_slug_is_detected_in_low_signal_request():
    skills = [
        {"name": "local-pi-delegation", "requires_toolsets": ["mcp__pi_worker__run_pi_task"]},
        {"name": "email-triage", "requires_toolsets": []},
    ]

    matched = _explicitly_named_skills(
        "Use $local-pi-delegation to do some work on my portfolio project.", skills
    )

    assert [skill["name"] for skill in matched] == ["local-pi-delegation"]


def test_proceed_anyway_inherits_the_pi_delegation_request():
    messages = [
        {
            "role": "user",
            "content": (
                "Use $local-pi-delegation in D:/Development/portfolio to inspect "
                "the repository and make the requested small change."
            ),
        },
        {
            "role": "assistant",
            "content": "The skill is not installed. Proceed anyway?",
        },
        {"role": "user", "content": "Proceed anyway"},
    ]

    assert _is_explicit_continuation("Proceed anyway")
    intent = _classify_agent_request(messages, "Proceed anyway")
    assert intent["continuation"]
    assert "local-pi-delegation" in intent["retrieval_query"]
    assert "D:/Development/portfolio" in intent["retrieval_query"]


# ── 5. a follow-up turn must not lose the tools the turn before it used ──────
#
# Second half of the same session: mid-audit, the user typed
#
#     "Continue searching there should be at least a hundred applications total"
#
# It isn't a bare "continue", so _EXPLICIT_CONTINUATION_RE (fully anchored)
# missed it; it names no email word, so no domain matched; low_signal fired,
# retrieval ran on that literal text, and every email tool the previous rounds
# had just been calling vanished. The agent then told the user it had no Gmail
# access. Widening the continuation regex with another phrase is a treadmill --
# what generalizes is that a tool this conversation already called stays
# eligible.

FOLLOWUP = "Continue searching there should be at least a hundred applications total"


def _apply_tool_retention(selected, messages, disabled=frozenset(), known=frozenset()):
    """Mirror of the retention step in stream_agent_loop.

    Same rationale as _apply_terminus_clamp above: the decision is a few lines
    buried inside a 700-line async generator that needs a live endpoint to
    reach, and it is worth pinning on its own.
    """
    prior = {t for t in _tools_used_in_conversation(messages, known) if t not in disabled}
    return set(selected) | prior


def _audit_conversation(latest=FOLLOWUP):
    return [
        {"role": "user", "content": "audit my inbox for job applications"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "audit_emails", "arguments": "{}"}},
            {"id": "c2", "type": "function",
             "function": {"name": "list_emails", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "42 matching email(s)"},
        {"role": "user", "content": latest},
    ]


def test_the_followup_really_does_look_like_a_new_low_signal_turn():
    """Pin the precondition: a follow-up that names no email and does not say
    "continue" still classifies without the email domain, so retention (below)
    remains the general fix."""
    bare = "There should be at least a hundred applications total"
    assert not _is_explicit_continuation(bare)
    intent = _classify_agent_request(_audit_conversation(bare), bare)
    assert "email" not in intent["domains"], (
        "if this ever starts matching, the retention fix is still the general one"
    )


def test_a_continue_with_tail_now_inherits_the_conversation_context():
    # 2026-09-13: "Continue with next slice ..." was a low-signal new request.
    # A leading continue/keep going/next slice now carries the prior work's
    # context, so the original FOLLOWUP routes back to the email tools too.
    assert not _is_explicit_continuation(FOLLOWUP)
    intent = _classify_agent_request(_audit_conversation(), FOLLOWUP)
    assert intent["continuation"] and "email" in intent["domains"]


def test_followup_turn_keeps_the_email_tools_the_conversation_was_using():
    messages = _audit_conversation()
    # What retrieval on the literal text produced: no email tool in sight.
    selected = {"ask_user", "update_plan", "manage_memory", "web_search"}

    result = _apply_tool_retention(selected, messages)

    assert {"audit_emails", "list_emails"} <= result, (
        "the agent lost the tools it had been calling and claimed no Gmail access"
    )
    assert selected <= result, "retention must add, never replace"


def test_fenced_tool_calls_are_retained_too():
    """Models without function calling call tools as ```<name>``` fences."""
    messages = [
        {"role": "user", "content": "audit my inbox"},
        {"role": "assistant", "content": "Scanning.\n```audit_emails\n{\"folder\": \"INBOX\"}\n```"},
        {"role": "user", "content": FOLLOWUP},
    ]

    result = _apply_tool_retention(set(), messages, known={"audit_emails", "bash"})

    assert "audit_emails" in result


def test_ordinary_code_fences_are_not_mistaken_for_tool_calls():
    messages = [
        {"role": "user", "content": "explain this"},
        {"role": "assistant", "content": "Here is the shape:\n```json\n{\"a\": 1}\n```"},
        {"role": "user", "content": "and now?"},
    ]

    assert _tools_used_in_conversation(messages, known={"audit_emails"}) == []


def test_retention_never_resurrects_a_disabled_tool():
    """An intentional restriction still wins over retention."""
    messages = _audit_conversation()

    result = _apply_tool_retention(set(), messages, disabled={"list_emails"})

    assert "audit_emails" in result
    assert "list_emails" not in result


def test_retained_set_is_capped_for_very_long_conversations():
    """A 60-round session must not accumulate an unbounded schema list."""
    messages = [{"role": "user", "content": "go"}]
    for i in range(60):
        messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function",
             "function": {"name": f"tool_{i}", "arguments": "{}"}},
        ]})
    messages.append({"role": "user", "content": FOLLOWUP})

    used = _tools_used_in_conversation(messages, known=frozenset())

    assert len(used) <= 24
    assert used[0] == "tool_59", "the most recently used tools are the ones kept"


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


# ── 5. the deep-research repro (2026-08-25) ──────────────────────────────────
#
#     "Do deep research. I need you to figure out why IOS devices could be
#      potentially disconnecting and reconnecting from my internet - Pi hole
#      has been configured to only using IPv4 as well as the router"
#
# Tool RAG retrieved `trigger_research` correctly. The turn also tripped the
# local-machine heuristic, so Terminus mode replaced the selection with the
# file/shell toolset and `trigger_research` was gone. The round opened with
# "Deep-research tooling is unavailable", made zero tool calls -- and the
# self-unblock never fired, because the missing-tool detector only knew the
# phrase "not available", never the single word "unavailable".

RESEARCH_REPRO = (
    "Do deep research. I need you to figure out why IOS devices could be "
    "potentially disconnecting and reconnecting from my internet - Pi hole has "
    "been configured to only using IPv4 as well as the router"
)


def test_research_request_is_recognised():
    assert _looks_like_research_request(RESEARCH_REPRO)
    assert _looks_like_research_request("research the best mechanical keyboards")
    assert _looks_like_research_request("look into why the deploy is flaky")
    assert not _looks_like_research_request("read the config file and fix the typo")


def test_retrieved_research_tool_survives_the_terminus_swap():
    """The exact drop that produced "deep-research tooling is unavailable"."""
    retrieved = {"trigger_research", "search_documents", "manage_settings"}
    result = _apply_terminus_clamp(
        retrieved, domains={"web", "settings", "files"}, query_matched=retrieved,
    )

    assert "trigger_research" in result, (
        "retrieval matched the deep-research tool for this query and the swap dropped it"
    )
    assert _WORKSPACE_TERMINUS_TOOLS <= result, "file/shell tools must still arrive"


@pytest.mark.parametrize("text", [
    "Deep-research tooling is unavailable. Preliminary diagnosis:",
    "Deep-research and Vault/AI Mind search tools are unavailable in this turn, "
    "so I cannot honestly claim researched findings from those sources.",
    "The research toolset is currently unavailable.",
])
def test_one_word_unavailable_excuses_are_detected(text):
    """Both real rounds phrased it as one word and slipped past every branch."""
    assert _claims_missing_tools(text), text


@pytest.mark.parametrize("text", [
    "Gmail returned Too many simultaneous connections, so the fetch failed.",
    "The Pi-hole admin API is unavailable right now (connection refused).",
    "I ran the tool and it returned an empty list.",
])
def test_real_upstream_failures_are_not_missing_tool_claims(text):
    """A round that reports a genuine failure must not re-arm and retry."""
    assert not _claims_missing_tools(text), text


# ── 6. a domain the selector emptied is restored, not announced as missing ───

def test_domain_starved_by_selection_is_restored():
    """"Pi hole has been configured ..." matched the settings domain; the swap
    then cleared it, and the turn opened by announcing what it could not do."""
    selected = set(_WORKSPACE_TERMINUS_TOOLS)
    starved = repair_starved_domains(selected, {"settings", "files"}, set())

    assert starved == [], "nothing is genuinely off, so nothing should be reported"
    assert _DOMAIN_TOOL_MAP["settings"] & selected, "settings tools should be back"


def test_domain_the_user_switched_off_is_still_reported():
    """A real restriction cannot be repaired, so the model must be told."""
    selected = set(_WORKSPACE_TERMINUS_TOOLS)
    disabled = set(_DOMAIN_TOOL_MAP["settings"])
    starved = repair_starved_domains(selected, {"settings"}, disabled)

    assert starved == ["settings"]
    assert not (_DOMAIN_TOOL_MAP["settings"] & selected)
    assert "settings" in _STARVED_DOMAIN_LABELS


def test_repair_respects_a_deliberate_clamp():
    """The Odysseus fine-tune modes mean the narrow toolset IS the behaviour."""
    selected = {"create_document", "ask_user"}
    starved = repair_starved_domains(
        selected, {"email"}, set(), allow_repair=False,
    )
    assert starved == ["email"]
    assert "list_emails" not in selected


def test_repair_respects_an_open_email_draft():
    """An open compose document drops the fetch tools on purpose."""
    selected = {"edit_document", "send_email"} - {"send_email"}
    starved = repair_starved_domains(
        selected, {"email"}, set(), protected={"email"},
    )
    assert starved == ["email"]
    assert "list_emails" not in selected


def test_web_domain_is_never_treated_as_starved():
    """Web tools are a per-turn user toggle, and "search"/"today" over-trigger it."""
    selected = {"read_file"}
    assert repair_starved_domains(selected, {"web"}, {"web_search", "web_fetch"}) == []


# ── The knowledge base, by every name the user calls it ────────────────
#
# Repro from a real session (2026-08-26):
#
#     "Edit the appropriate job report in AI Mind please"
#
# Only the literal word "vault" seeded the files domain, so this matched
# `documents` on the word "edit" and nothing else. The turn was handed
# create_document/edit_document — Odysseus's own document store, a different
# place from the files on disk — and round 1 replied that the workspace file
# tools "aren't available", made zero tool calls, and ended.

VAULT_REPRO = "Edit the appropriate job report in AI Mind please"

VAULT_ALIASES = [
    "AI Mind", "AI-Mind", "ai mind",
    "vault", "the vault", "vault mind", "mind vault",
    "obsidian", "Obsidian",
    "knowledge base", "knowledge-base", "knowledgebase",
]


@pytest.mark.parametrize("alias", VAULT_ALIASES)
def test_every_name_for_the_knowledge_base_reads_as_file_work(alias):
    assert _looks_like_vault_request(f"edit the job report in {alias} please")


@pytest.mark.parametrize("alias", VAULT_ALIASES)
def test_every_name_for_the_knowledge_base_seeds_the_files_domain(alias):
    domains = _classify_agent_request([], f"edit the job report in {alias} please")["domains"]
    assert "files" in domains


def test_the_repro_now_reaches_tools_that_can_actually_edit_a_file():
    domains = _classify_agent_request([], VAULT_REPRO)["domains"]
    assert {"documents", "files"} <= domains

    # What retrieval gave the failing turn: document tools only.
    selected = {"create_document", "edit_document", "update_document", "manage_documents"}
    merged = apply_terminus_toolset(selected, query_matched=set(), domains=domains)

    assert {"read_file", "edit_file", "write_file", "ls", "grep"} <= merged
    assert selected <= merged, "naming the vault must ADD file tools, not replace the doc tools"


def test_naming_the_vault_does_not_need_a_bound_workspace():
    """The knowledge base has absolute paths; the workspace-coding gate is a
    separate, narrower signal that this request never satisfied."""
    assert _looks_like_vault_request(VAULT_REPRO)
    assert not _looks_like_workspace_coding_request(VAULT_REPRO)


def test_edit_counts_as_a_workspace_action_verb():
    """`update` and `change` were actions; `edit`/`write`/`append` were not."""
    for verb in ("edit", "write", "append", "update"):
        assert _looks_like_workspace_coding_request(f"{verb} the config file"), verb


# ── The self-unblock has to recognise the claim it was built for ───────


def test_a_clause_between_tools_and_the_negation_still_counts():
    """The exact sentence that ended the failing turn."""
    assert _claims_missing_tools(
        "I can't directly edit the AI Mind vault file in this turn because the "
        "workspace file tools needed to read/write `/app/workspace/AI Mind/...` "
        "aren't available."
    )


@pytest.mark.parametrize("claim", [
    "The file tools I would need here are not available.",
    "The email tools for that are not enabled.",
    "the tools required to do this aren't accessible",
])
def test_gapped_tool_claims_are_caught(claim):
    assert _claims_missing_tools(claim)


@pytest.mark.parametrize("not_a_claim", [
    "Gmail returned Too many simultaneous connections; I stopped after two retries.",
    "The tools ran but the API is not responding right now.",
    "That file is not available in the repo.",
    "The search results are not available for that date range.",
])
def test_a_real_upstream_failure_still_does_not_re_arm(not_a_claim):
    """Re-arming on a genuine failure would loop the turn, not unblock it."""
    assert not _claims_missing_tools(not_a_claim)
