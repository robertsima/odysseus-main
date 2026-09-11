"""ask_teacher must not give up on the first failing teacher, and list_models
must find a model from a multi-word filter.

From the 2026-09-11 logs: `ask_teacher default` → "Model 'default' not found";
`list_models claude opus` → nothing (ids are hyphenated); `ask_teacher
gpt-6-astra` → HTTP 400 → the agent carried on without any teacher.
"""
import asyncio

import src.ai_interaction as ai_interaction
import src.llm_core as llm_core
from src.agent_tools import model_interaction_tools as mit


def _settings(monkeypatch, teacher=""):
    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: teacher if key == "teacher_model" else default)


def test_requested_teacher_failure_falls_back_to_configured_teacher(monkeypatch):
    _settings(monkeypatch, teacher="big-teacher")
    calls = []

    def fake_resolve(spec, owner=None):
        return ("http://x", spec, {})

    async def fake_call(url, model, messages, headers=None, timeout=None):
        calls.append(model)
        if model == "gpt-6-astra":
            raise RuntimeError("400: Unsupported parameter: temperature")
        return "use the other path"

    monkeypatch.setattr(ai_interaction, "_resolve_model", fake_resolve)
    monkeypatch.setattr(llm_core, "llm_call_async", fake_call)

    res = asyncio.run(mit.AskTeacherTool().execute("gpt-6-astra\nI am stuck", {"owner": "bob"}))

    assert calls == ["gpt-6-astra", "big-teacher"]
    assert res["teacher"] is True and res["model"] == "big-teacher"
    assert res["response"] == "use the other path"
    assert "gpt-6-astra" in res["note"] and "big-teacher" in res["note"]


def test_unresolvable_teacher_falls_back_to_configured_teacher(monkeypatch):
    _settings(monkeypatch, teacher="big-teacher")

    def fake_resolve(spec, owner=None):
        if spec != "big-teacher":
            raise ValueError(f"Model '{spec}' not found on any configured endpoint")
        return ("http://x", "big-teacher", {})

    async def fake_call(url, model, messages, headers=None, timeout=None):
        return "answer"

    monkeypatch.setattr(ai_interaction, "_resolve_model", fake_resolve)
    monkeypatch.setattr(llm_core, "llm_call_async", fake_call)

    res = asyncio.run(mit.AskTeacherTool().execute("nonexistent-model\nhelp", {}))
    assert res["model"] == "big-teacher" and res["response"] == "answer"


def test_default_alias_means_the_configured_teacher(monkeypatch):
    _settings(monkeypatch, teacher="big-teacher")
    seen = []

    def fake_resolve(spec, owner=None):
        seen.append(spec)
        return ("http://x", spec, {})

    async def fake_call(url, model, messages, headers=None, timeout=None):
        return "ok"

    monkeypatch.setattr(ai_interaction, "_resolve_model", fake_resolve)
    monkeypatch.setattr(llm_core, "llm_call_async", fake_call)

    res = asyncio.run(mit.AskTeacherTool().execute("default\nhelp", {}))
    assert seen == ["big-teacher"], "'default' is an alias for the configured teacher, not a model id"
    assert res["response"] == "ok" and "note" not in res


def test_every_candidate_failing_reports_what_was_tried(monkeypatch):
    _settings(monkeypatch, teacher="big-teacher")

    def fake_resolve(spec, owner=None):
        return ("http://x", spec, {})

    async def fake_call(url, model, messages, headers=None, timeout=None):
        raise RuntimeError(f"{model} is down")

    monkeypatch.setattr(ai_interaction, "_resolve_model", fake_resolve)
    monkeypatch.setattr(llm_core, "llm_call_async", fake_call)

    res = asyncio.run(mit.AskTeacherTool().execute("gpt-6-astra\nhelp", {}))
    assert "error" in res
    assert "gpt-6-astra: gpt-6-astra is down" in res["error"]
    assert "big-teacher: big-teacher is down" in res["error"]
    assert "list_models" in res["error"]


def test_no_configured_teacher_and_auto_is_an_actionable_error(monkeypatch):
    _settings(monkeypatch, teacher="")
    res = asyncio.run(mit.AskTeacherTool().execute("auto\nhelp", {}))
    assert "No teacher model configured" in res["error"]
    assert "list_models" in res["error"]


def test_list_models_filter_matches_every_term_across_hyphens():
    terms = mit._keyword_terms("claude opus")
    assert terms == ["claude", "opus"]
    assert mit._matches_terms("claude-opus-4-7", "Anthropic", terms)
    assert mit._matches_terms("opus-4-7", "Claude via OpenRouter", terms), "terms may match the endpoint name"
    assert not mit._matches_terms("claude-sonnet-4-6", "Anthropic", terms)
    assert mit._matches_terms("gpt-6-astra", "ChatGPT Subscription", mit._keyword_terms("gpt-6"))
    assert mit._keyword_terms("") == [] and mit._keyword_terms(None) == []
