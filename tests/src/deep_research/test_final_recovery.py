"""Final synthesis recovers locally without losing evidence or claiming success."""
import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from src.deep_research import DeepResearcher


DRAFT = "Buyer interviews identified a concrete need. [Source](https://example.test/research)"
FINAL = "## Evidence\n\n" + " ".join(f"finding{n}" for n in range(410))


def researcher_with_responses(*responses):
    events = []
    researcher = DeepResearcher(
        llm_endpoint="http://research.test/v1/chat/completions",
        llm_model="configured-research-model",
        progress_callback=events.append,
    )
    researcher.findings = [
        {"title": "Interview", "url": "https://example.test/research", "summary": "A concrete buyer need."}
    ]
    calls = []

    async def fake_llm(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        response = responses[len(calls) - 1]
        if isinstance(response, BaseException):
            raise response
        return response

    researcher._llm = fake_llm
    return researcher, calls, events


def test_repetition_provider_failure_retries_only_final_synthesis_at_lower_temperature():
    researcher, calls, events = researcher_with_responses(
        HTTPException(502, "Stopped generation: model started repeating tokens (ne ne ne ne)."),
        FINAL,
    )

    result = asyncio.run(researcher._final_report("Who buys this?", DRAFT))

    assert result == FINAL
    assert len(calls) == 2
    assert calls[1]["temperature"] < calls[0]["temperature"]
    assert all(DRAFT in call["messages"][0]["content"] for call in calls)
    assert all(len(call["messages"]) == 1 for call in calls)
    assert researcher.final_report_metadata == {
        "status": "complete", "attempts": 2, "last_failure": "repetitive_output"
    }
    assert any(event.get("synthesis_status") == "retrying" for event in events)
    assert researcher.queries_used == set()
    assert researcher.findings[0]["url"] == "https://example.test/research"


@pytest.mark.parametrize("bad_output", [None, "", "  ", "<think>internal reasoning</think>", "ne " * 100])
def test_empty_or_degenerate_outputs_retry_without_echoing_bad_text(bad_output):
    researcher, calls, _ = researcher_with_responses(bad_output, FINAL)

    assert asyncio.run(researcher._final_report("Question", DRAFT)) == FINAL
    assert len(calls) == 2
    assert len(calls[1]["messages"]) == 1
    assert "The previous generation failed" in calls[1]["messages"][0]["content"]


@pytest.mark.parametrize("error", [TimeoutError(), ConnectionError(), httpx.ReadTimeout("read timeout")])
def test_transient_failures_get_one_recovery_attempt(error):
    researcher, calls, _ = researcher_with_responses(error, FINAL)

    assert asyncio.run(researcher._final_report("Question", DRAFT)) == FINAL
    assert len(calls) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_nonretryable_provider_errors_preserve_draft_with_partial_status(status):
    researcher, calls, events = researcher_with_responses(HTTPException(status, "private provider detail"))

    result = asyncio.run(researcher._final_report("Question", DRAFT))

    assert len(calls) == 1
    assert result.startswith("**Partial research")
    assert DRAFT in result
    assert "private provider detail" not in result
    assert researcher.get_stats()["Synthesis"] == "partial"
    assert researcher.get_stats()["Synthesis failure"] == f"provider_http_{status}"
    assert events[-1]["synthesis_status"] == "partial"


def test_two_failed_generations_preserve_evidence_and_are_not_marked_complete():
    researcher, calls, _ = researcher_with_responses(HTTPException(502, "repeating tokens"), "")

    result = asyncio.run(researcher._final_report("Question", DRAFT))

    assert len(calls) == 2
    assert DRAFT in result
    assert "not a completed final report" in result
    assert researcher.final_report_metadata["failure"] == "empty_output"
    assert researcher.get_stats()["Synthesis"] == "partial"


def test_invalid_saved_draft_falls_back_to_collected_findings():
    researcher, calls, _ = researcher_with_responses("", "")

    result = asyncio.run(researcher._final_report("Question", "ne " * 100))

    assert len(calls) == 2
    assert "A concrete buyer need." in result
    assert "https://example.test/research" in result
    assert "ne ne ne" not in result


@pytest.mark.parametrize("expansion", [HTTPException(502, "repeating tokens"), "", "ne " * 100])
def test_failed_optional_expansion_preserves_valid_short_final_report(expansion):
    researcher, calls, events = researcher_with_responses(DRAFT, expansion)

    result = asyncio.run(researcher._final_report("Question", "Earlier findings"))

    assert result == DRAFT
    assert len(calls) == 2
    assert researcher.final_report_metadata["status"] == "complete"
    assert researcher.final_report_metadata["expansion_failure"]
    assert any("shorter report" in event.get("message", "") for event in events)


def test_short_successful_recovery_does_not_trigger_a_third_generation():
    researcher, calls, _ = researcher_with_responses("", DRAFT)

    assert asyncio.run(researcher._final_report("Question", "Earlier findings")) == DRAFT
    assert len(calls) == 2


def test_successful_expansion_is_kept():
    researcher, calls, _ = researcher_with_responses(DRAFT, FINAL)

    assert asyncio.run(researcher._final_report("Question", "Earlier findings")) == FINAL
    assert len(calls) == 2


def test_full_length_success_does_not_make_an_unnecessary_second_call():
    researcher, calls, _ = researcher_with_responses(FINAL)

    assert asyncio.run(researcher._final_report("Question", DRAFT)) == FINAL
    assert len(calls) == 1


def test_final_attempts_share_a_wall_clock_budget(monkeypatch):
    import src.deep_research as module

    clock = [1000.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    researcher, _, _ = researcher_with_responses()
    calls = []

    async def slow_failure(messages, **kwargs):
        calls.append(kwargs)
        clock[0] += 181
        raise HTTPException(503, "unavailable")

    researcher._llm = slow_failure
    result = asyncio.run(researcher._final_report("Question", DRAFT))

    assert len(calls) == 1
    assert calls[0]["timeout"] <= 180
    assert result.startswith("**Partial research")


def test_cancelled_research_does_not_launch_final_generation():
    researcher, calls, _ = researcher_with_responses()
    researcher.cancel()

    result = asyncio.run(researcher._final_report("Question", DRAFT))

    assert calls == []
    assert DRAFT in result
    assert researcher.get_stats()["Synthesis"] == "cancelled"


def test_task_cancellation_propagates_without_retry():
    researcher, calls, _ = researcher_with_responses(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(researcher._final_report("Question", DRAFT))
    assert len(calls) == 1


@pytest.mark.parametrize("bad_output", ["", "ne " * 100])
def test_intermediate_synthesis_cannot_replace_good_evidence_with_invalid_output(bad_output):
    researcher, calls, _ = researcher_with_responses(bad_output)

    result = asyncio.run(researcher._synthesize("Question", researcher.findings, DRAFT))

    assert result == DRAFT
    assert len(calls) == 1


def test_research_with_no_synthesized_draft_preserves_findings_and_partial_metadata():
    researcher, calls, _ = researcher_with_responses("Unused")
    researcher.max_rounds = 0

    async def create_plan(question):
        return "Plan"

    async def classify(question):
        return None

    researcher._create_plan = create_plan
    researcher._classify_category = classify
    result = asyncio.run(researcher.research("Question", prior_findings=researcher.findings))

    assert calls == []
    assert "A concrete buyer need." in result
    assert researcher.get_stats()["Synthesis"] == "partial"
    assert researcher.get_stats()["Synthesis failure"] == "synthesis_unavailable"
