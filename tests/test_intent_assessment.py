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
