"""Prompt audit A4 (2026-10-01): research prompts and the shared auto-sort prompt."""
import asyncio
import json
import sys
import types

import pytest

from src import deep_research
from src.deep_research import DeepResearcher
from src.goal_based_extractor import EXTRACTOR_SYSTEM
from src.session_actions import build_auto_sort_prompt


def _researcher():
    return DeepResearcher(
        llm_endpoint="http://local.test/v1/chat/completions",
        llm_model="local-model",
    )


def _stub_page(monkeypatch):
    search_mod = types.ModuleType("src.search")
    search_mod.fetch_webpage_content = lambda url, timeout: {
        "success": True, "content": "page text", "title": "Page", "og_image": "",
    }
    monkeypatch.setitem(sys.modules, "src.search", search_mod)

    async def immediate(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", immediate)


def test_extractor_asks_only_for_fields_the_pipeline_reads():
    prompt = EXTRACTOR_SYSTEM.format(goal="g")
    assert '"relevant"' in prompt and '"summary"' in prompt
    assert "rational" not in prompt and '"evidence"' not in prompt
    # Figures must survive into the one summary the final report sees.
    assert "figures" in prompt and "prices" in prompt


@pytest.mark.asyncio
async def test_page_marked_irrelevant_is_dropped(monkeypatch):
    _stub_page(monkeypatch)
    researcher = _researcher()

    async def fake_llm(messages, **_kw):
        return json.dumps({"relevant": False, "summary": ""})

    researcher._llm = fake_llm
    assert await researcher._fetch_and_extract("https://x.test", "q", "T") is None


@pytest.mark.asyncio
async def test_relevant_page_keeps_summary_with_figures(monkeypatch):
    _stub_page(monkeypatch)
    researcher = _researcher()

    async def fake_llm(messages, **_kw):
        return json.dumps({"relevant": True, "summary": "The Pro plan costs $12 per user per month."})

    researcher._llm = fake_llm
    finding = await researcher._fetch_and_extract("https://x.test", "q", "T")
    assert "$12" in researcher._format_findings([finding])


def test_final_report_prompt_has_no_word_floor_or_shouting():
    prompt = deep_research.FINAL_REPORT_PROMPT
    assert "MINIMUM" not in prompt
    assert "1500" not in prompt
    for block in deep_research.CATEGORY_PROMPTS.values():
        assert "IMPORTANT FORMAT OVERRIDE" not in block
        assert "replaces the summary-first layout" in block


def test_plan_example_does_not_anchor_on_one_topic():
    assert "cost of living" not in deep_research.RESEARCH_PLAN_PROMPT
    assert "healthcare" not in deep_research.RESEARCH_PLAN_PROMPT


def test_stop_prompt_has_no_continue_bias():
    assert "prefer continuing" not in deep_research.STOP_PROMPT


def test_auto_sort_prompt_lists_existing_folders():
    sessions = [{"id": "abcdef123456", "name": "Pasta night", "current_folder": None}]
    prompt = build_auto_sort_prompt(sessions, ["Cooking", "Travel"])
    assert 'Existing folders: "Cooking", "Travel"' in prompt
    assert '"abcdef12": "Pasta night"' in prompt
    assert "existing folder when it fits" in prompt


def test_auto_sort_prompt_without_folders_says_so():
    prompt = build_auto_sort_prompt([{"id": "abcdef123456", "name": "x", "current_folder": None}], [])
    assert "Existing folders: none yet" in prompt


def test_tidy_button_and_scheduled_sweep_share_one_prompt_builder():
    import inspect
    import routes.session_routes as sr
    import src.session_actions as sa

    for mod in (sr, sa):
        source = inspect.getsource(mod)
        assert "build_auto_sort_prompt(" in source
        assert "You are a session organizer" not in source
