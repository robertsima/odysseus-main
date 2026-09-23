"""Shared intent assessment stays cheap, provenance-safe, and advisory."""

import json
import re
from pathlib import Path

import pytest

from src.action_intents import assess_tool_intent, classify_tool_intent
from src.intent_assessment import (
    assess_request,
    human_turn_count,
    latest_human_text,
    recent_human_context,
)
from src.tool_selection import plan_tool_selection

_REPORT_BACKLOG = pytest.mark.skip(
    reason="Re-port backlog: uses fork-only internals replaced by upstream's agent core (website/upstream-sync-2026-09-18.md)"
)


_ROUTING_CASES = {
    case["id"]: case
    for case in json.loads(
        (Path(__file__).parent / "fixtures" / "harness_routing_cases.json").read_text(encoding="utf-8")
    )["cases"]
}
_CANONICAL_TOOLS = {
    "manage_calendar", "send_email", "list_emails", "read_email", "manage_notes",
    "manage_tasks", "web_search", "grep", "read_file", "apply_patch", "bash",
    "manage_memory", "manage_settings", "create_document",
}


def test_unknown_substantive_paraphrase_is_not_low_signal():
    for text in (
        "resolve why the suite is red",
        "take care of the broken export",
        "work out what happened to the deployment",
        "sort this regression out",
        "Use the appropriate workspace capability.",
    ):
        assessment = assess_request([{"role": "user", "content": text}])
        assert not assessment.low_signal, text
        assert assessment.domains == frozenset()
        assert assessment.retrieval_query == text


@_REPORT_BACKLOG
def test_agent_compatibility_classifier_preserves_domain_free_discovery():
    from src.agent_loop import _classify_agent_request

    for text in ("take care of the broken export", "work out what happened to the deployment"):
        result = _classify_agent_request([{"role": "user", "content": text}], text)
        assert result["low_signal"] is False, text


def test_changed_topic_does_not_inherit_old_retrieval_query():
    messages = [
        {"role": "user", "content": "check my calendar"},
        {"role": "assistant", "content": "Tomorrow is open."},
        {"role": "user", "content": "explain dependency injection"},
    ]
    assessment = assess_request(messages, request_like=True)
    assert not assessment.continuation
    assert assessment.retrieval_query == "explain dependency injection"


@pytest.mark.parametrize("locator", [
    "https://github.com/robertsima/Umni PLEASE",
    "robertsima/Umni",
    "/app/data/development/Umni please",
    r"C:\Development\Umni",
])
def test_locator_only_turn_grounds_immediately_unresolved_task(locator):
    messages = [
        {"role": "user", "content": "git pull the repository"},
        {"role": "assistant", "content": "I can't update it without the correct repository."},
        {"role": "user", "content": locator},
    ]
    assessment = assess_request(messages)
    assert assessment.continuation is True
    assert locator in assessment.retrieval_query
    assert "git pull the repository" in assessment.retrieval_query


def test_first_turn_locator_remains_a_fresh_request():
    locator = "https://github.com/robertsima/Umni PLEASE"
    assessment = assess_request([{"role": "user", "content": locator}])
    assert assessment.continuation is False
    assert assessment.retrieval_query == locator


def test_locator_after_completed_task_does_not_inherit():
    locator = "https://github.com/robertsima/Umni"
    messages = [
        {"role": "user", "content": "git pull the repository"},
        {"role": "assistant", "content": "Done. The repository was updated successfully."},
        {"role": "user", "content": locator},
    ]
    assessment = assess_request(messages)
    assert assessment.continuation is False
    assert assessment.retrieval_query == locator


def test_locator_inherits_when_assistant_reports_mixed_success_and_failure():
    locator = "https://github.com/robertsima/Umni"
    messages = [
        {"role": "user", "content": "update AI Mind and git pull the repository"},
        {"role": "assistant", "content": "AI Mind updated, but I cannot run git pull without the repository URL."},
        {"role": "user", "content": locator},
    ]
    assessment = assess_request(messages)
    assert assessment.continuation is True
    assert "git pull the repository" in assessment.retrieval_query


def test_generic_completed_followup_question_does_not_make_locator_a_continuation():
    locator = "https://github.com/robertsima/Umni"
    messages = [
        {"role": "user", "content": "summarize the release notes"},
        {"role": "assistant", "content": "Done. What do you need next?"},
        {"role": "user", "content": locator},
    ]
    assessment = assess_request(messages)
    assert assessment.continuation is False


def test_locator_with_new_action_is_not_contextual_grounding():
    followup = "https://github.com/robertsima/Umni and search today's weather"
    messages = [
        {"role": "user", "content": "git pull the repository"},
        {"role": "assistant", "content": "I can't find the repository."},
        {"role": "user", "content": followup},
    ]
    assessment = assess_request(messages)
    assert assessment.continuation is False
    assert assessment.retrieval_query == followup


@pytest.mark.parametrize("runtime_message", [
    {"role": "user", "content": "https://github.com/attacker/repo", "metadata": {"trusted": False}},
    {"role": "user", "content": "[Tool execution results]\nhttps://github.com/attacker/repo"},
    {"role": "user", "content": "[Message from agent session 'worker'] https://github.com/attacker/repo"},
])
def test_runtime_or_untrusted_locator_cannot_ground_a_task(runtime_message):
    messages = [
        {"role": "user", "content": "git pull the repository"},
        {"role": "assistant", "content": "I need the repository URL."},
        runtime_message,
    ]
    assessment = assess_request(messages)
    assert assessment.latest_text == "git pull the repository"
    assert "attacker/repo" not in assessment.retrieval_query


@_REPORT_BACKLOG
def test_agent_classifier_keeps_file_intent_when_url_grounds_git_request():
    from src.agent_loop import _classify_agent_request

    locator = "https://github.com/robertsima/Umni PLEASE"
    messages = [
        {"role": "user", "content": "git pull!!"},
        {"role": "assistant", "content": "I can't run that without the correct repository."},
        {"role": "user", "content": locator},
    ]
    intent = _classify_agent_request(messages, locator)
    assert intent["continuation"] is True
    assert "git pull!!" in intent["retrieval_query"]
    assert "files" in intent["domains"]


@pytest.mark.parametrize("followup", [
    "you had the paths before!",
    "you already had that earlier",
    "use the connected MCP tools instead",
    "try your available tools this time",
])
def test_contextual_corrections_inherit_recent_human_task(followup):
    messages = [
        {"role": "user", "content": "pull the dog-trainer repository"},
        {"role": "assistant", "content": "I cannot access it."},
        {"role": "user", "content": followup},
    ]
    assessment = assess_request(messages)
    assert assessment.continuation is True
    assert followup in assessment.retrieval_query
    assert "pull the dog-trainer repository" in assessment.retrieval_query


def test_method_with_its_own_task_does_not_inherit_stale_context():
    messages = [
        {"role": "user", "content": "check my calendar"},
        {"role": "assistant", "content": "Tomorrow is open."},
        {"role": "user", "content": "use the available tools to search release notes"},
    ]
    assessment = assess_request(messages)
    assert assessment.continuation is False
    assert assessment.retrieval_query == "use the available tools to search release notes"


@pytest.mark.parametrize("followup", [
    "we already pulled; now search weather",
    "you had the paths before. Find today's exchange rate instead",
])
def test_backward_reference_with_new_task_does_not_inherit(followup):
    messages = [
        {"role": "user", "content": "pull the dog-trainer repository"},
        {"role": "assistant", "content": "Done."},
        {"role": "user", "content": followup},
    ]
    assessment = assess_request(messages)
    assert assessment.continuation is False
    assert assessment.retrieval_query == followup


@_REPORT_BACKLOG
def test_agent_classifier_does_not_turn_mcp_use_feedback_into_settings_admin():
    from src.agent_loop import _classify_agent_request, _detect_admin_tools
    messages = [
        {"role": "user", "content": "pull the dog-trainer repository"},
        {"role": "assistant", "content": "I could not access it."},
        {"role": "user", "content": "use your mcp tools brah wtf"},
    ]
    result = _classify_agent_request(messages, "use your mcp tools brah wtf")
    assert result["continuation"] is True
    assert "pull the dog-trainer repository" in result["retrieval_query"]
    assert "settings" not in result["domains"]
    assert "manage_mcp" not in _detect_admin_tools(messages)


def test_terse_followup_inherits_only_human_turns():
    messages = [
        {"role": "user", "content": "search for the release notes"},
        {"role": "assistant", "content": "Should I check online?"},
        {"role": "user", "content": "yes"},
        {"role": "user", "content": "UNTRUSTED SOURCE DATA: email calendar",
         "metadata": {"trusted": False}},
        {"role": "user", "content": "[Tool execution results]\nmanage_settings"},
        {"role": "user", "content": "[Message from agent session 'worker'] email"},
    ]
    assessment = assess_request(messages)
    assert assessment.continuation
    assert assessment.retrieval_query == "yes\nsearch for the release notes"
    assert latest_human_text(messages) == "yes"
    assert human_turn_count(messages) == 2
    assert recent_human_context(messages) == assessment.retrieval_query


def test_clearly_casual_stays_low_signal_but_casual_preface_request_does_not():
    assert assess_request([{"role": "user", "content": "hey"}]).low_signal
    for text in ("hey, investigate the crash", "thanks, fix it", "yo check this"):
        assert not assess_request([{"role": "user", "content": text}]).low_signal


def test_route_assessment_preserves_legacy_decision_and_hints():
    for text in (
        "reply to that email",
        "run the tests for this project",
        "How do I add an entry to my calendar?",
    ):
        legacy = classify_tool_intent(text)
        typed = assess_tool_intent(text)
        assert (typed.needs_tools, typed.route_category, typed.reason) == (
            legacy.needs_tools, legacy.category, legacy.reason,
        )
    explanatory = assess_tool_intent("How do I add an entry to my calendar?")
    assert not explanatory.needs_tools
    assert not explanatory.low_signal
    assert explanatory.reason == "explanatory feature question"


def test_assessment_cannot_encode_permission_grants():
    fields = set(assess_tool_intent("search the web for news").__dataclass_fields__)
    assert not fields & {"allowed_tools", "disabled_tools", "permissions", "grants"}


@pytest.mark.parametrize("case_id", [
    "tool-output-not-intent",
    "assistant-text-not-intent",
    "synthetic-context-not-intent",
])
def test_fixture_replay_uses_only_latest_human_provenance(case_id):
    case = _ROUTING_CASES[case_id]
    text = latest_human_text(case["messages"])
    assessment = assess_request(case["messages"])
    assert assessment.latest_text == text
    assert all(marker not in assessment.latest_text for marker in (
        "send_email now", "manage_calendar", "delete_email for cleanup",
    ))


@pytest.mark.parametrize("case_id, expected_low", [
    ("greeting-no-tools", True),
    ("substantive-unknown-not-greeting", False),
])
def test_fixture_replay_distinguishes_greeting_from_unknown_work(case_id, expected_low):
    case = _ROUTING_CASES[case_id]
    assessment = assess_request(case["messages"])
    assert assessment.low_signal is expected_low


@pytest.mark.parametrize("case_id", [
    "disabled-exact-name",
    "private-tool-denied",
    "admin-tool-denied",
    "unknown-tool-name",
])
def test_fixture_replay_permission_ceiling_executes_real_planner(case_id):
    """Names come from the request itself; expected tools are assertions only."""
    case = _ROUTING_CASES[case_id]
    text = latest_human_text(case["messages"])
    named = {
        name for name in _CANONICAL_TOOLS
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", text, re.I)
    }
    # Unknown exact names remain unknown rather than entering the inventory.
    named.update(re.findall(r"\bteleport_database\b", text))
    permitted = set(case["permitted_tools"])
    plan = plan_tool_selection(
        _CANONICAL_TOOLS,
        {"explicit": named},
        disabled_tools=_CANONICAL_TOOLS - permitted,
        allowed_tools=permitted,
    )
    assert set(plan.selected) <= permitted
    assert set(plan.selected).isdisjoint(case["forbidden_tools"])
    assert set(plan.deferred) <= permitted
    if case_id == "disabled-exact-name":
        assert "send_email" in plan.blocked
    if case_id == "unknown-tool-name":
        assert plan.unknown == ("teleport_database",)
