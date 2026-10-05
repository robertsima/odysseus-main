"""Research terminal transport state stays compatible; report outcome is explicit."""
import asyncio
import json
import logging

import pytest

from src.deep_research import DeepResearcher
from src.research_handler import ResearchHandler


@pytest.fixture
def handler(monkeypatch, tmp_path):
    import src.research_handler as module

    monkeypatch.setattr(module, "RESEARCH_DATA_DIR", tmp_path)
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    monkeypatch.setattr("src.event_bus.fire_event", lambda *args, **kwargs: None)
    handler = ResearchHandler.__new__(ResearchHandler)
    handler._active_tasks = {}
    handler._legacy_engine = None

    async def probe(*args, **kwargs):
        pass

    monkeypatch.setattr(handler, "_probe_endpoint", probe)
    return handler


def patch_research_outcome(monkeypatch, outcome):
    async def research(self, question, **kwargs):
        self.final_report_metadata = {"status": outcome, "attempts": 2}
        if outcome != "complete":
            self.final_report_metadata["failure"] = "repetitive_output"
        self.findings = [{"title": "Source", "url": "https://example.test", "summary": "Evidence gathered."}]
        return "**Partial research — final synthesis did not complete.**" if outcome != "complete" else "Final report."

    monkeypatch.setattr(DeepResearcher, "research", research)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["partial", "failed", "complete"])
async def test_service_logs_success_only_when_synthesis_completed(handler, monkeypatch, caplog, outcome):
    patch_research_outcome(monkeypatch, outcome)
    entry = {}
    events = []

    with caplog.at_level(logging.INFO, logger="src.research_handler"):
        await handler.call_research_service(
            "Question", "http://research.test", "model", _task_entry=entry, progress_callback=events.append
        )

    assert entry["outcome"] == outcome
    assert entry["synthesis"]["status"] == outcome
    assert ("IterResearch completed successfully" in caplog.text) == (outcome == "complete")
    if outcome != "complete":
        assert events[-1]["phase"] == "warning"
        assert events[-1]["outcome"] == outcome


@pytest.mark.asyncio
async def test_partial_outcome_survives_persistence_and_keeps_result_fetch_compatible(handler, monkeypatch, tmp_path):
    patch_research_outcome(monkeypatch, "partial")
    callbacks = []

    handler.start_research("partial-run", "Question", "http://research.test", "model", hard_timeout=60,
                           on_complete=lambda *args: callbacks.append(args))
    await handler._active_tasks["partial-run"]["task"]

    status = handler.get_status("partial-run")
    assert status["status"] == "done"  # existing chat/research clients fetch only terminal 'done'
    assert status["outcome"] == "partial"
    assert status["synthesis"]["failure"] == "repetitive_output"
    assert "Partial research" in handler.get_result("partial-run")
    assert len(callbacks) == 1
    saved = json.loads((tmp_path / "partial-run.json").read_text(encoding="utf-8"))
    assert saved["outcome"] == "partial"
    assert saved["synthesis"] == status["synthesis"]
    assert saved["raw_findings"][0]["summary"] == "Evidence gathered."
    assert handler.get_avg_duration() is None  # failed synthesis is not a success-duration sample

    handler._active_tasks.clear()
    restored = handler.get_status("partial-run")
    assert restored["status"] == "done"
    assert restored["outcome"] == "partial"
    assert restored["synthesis"] == saved["synthesis"]
    assert "Partial research" in handler.get_result("partial-run")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.TimeoutError(), RuntimeError("background error")])
@pytest.mark.parametrize("with_draft", [False, True])
async def test_interrupted_background_job_preserves_partial_findings(handler, monkeypatch, error, with_draft):
    researcher = DeepResearcher("http://research.test", "model")
    researcher.evolving_report = "Preserved draft with citations." if with_draft else ""
    researcher.findings = [{"title": "Source", "url": "https://example.test", "summary": "Evidence gathered."}]
    callbacks = []

    async def interrupted(*args, _task_entry, **kwargs):
        _task_entry["researcher"] = researcher
        raise error

    monkeypatch.setattr(handler, "call_research_service", interrupted)
    handler.start_research("interrupted", "Question", "http://research.test", "model", hard_timeout=60,
                           on_complete=lambda *args: callbacks.append(args))
    await handler._active_tasks["interrupted"]["task"]

    entry = handler._active_tasks["interrupted"]
    assert entry["status"] == "done"
    assert entry["outcome"] == "partial"
    assert entry["stats"]["Synthesis"] == "partial"
    assert "not a completed final report" in handler.get_result("interrupted")
    assert ("Preserved draft" if with_draft else "Evidence gathered.") in entry["raw_report"]
    assert len(callbacks) == 1


@pytest.mark.asyncio
async def test_interrupted_job_without_evidence_reports_failed_outcome(handler, monkeypatch):
    async def interrupted(*args, **kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(handler, "call_research_service", interrupted)
    handler.start_research("empty-run", "Question", "http://research.test", "model", hard_timeout=60)
    await handler._active_tasks["empty-run"]["task"]

    status = handler.get_status("empty-run")
    assert status["status"] == "error"
    assert status["outcome"] == "failed"
    assert status["synthesis"]["failure"] == "hard_timeout"
