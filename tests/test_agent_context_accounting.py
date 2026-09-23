"""Native tool schemas and cache usage must be visible in context metrics.

Also pins that a headless run (worker / sub-agent) reaches the agent loop with a
real context window. It used to arrive with 0, which is not a window at all:
``context_profiles.preset_for_window(0)`` reads "balanced", so a 400k worker got
half the tool-output inline limit a chat on the same model gets, and every
metric derived from the window (``context_percent``) read 0.
"""

import pytest

pytest.skip(
    "Re-port backlog: exercises the fork's agent loop, replaced by upstream's in the 2026-09-18 sync (website/upstream-sync-2026-09-18.md)",
    allow_module_level=True,
)


import asyncio

from src.agent_loop import _compute_final_metrics, _estimate_tool_schema_tokens
from src.model_context import estimate_tokens


def test_schema_estimate_adds_real_request_overhead():
    messages = [{"role": "user", "content": "hello"}]
    schemas = [{
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from disk",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    }]

    schema_tokens = _estimate_tool_schema_tokens(schemas)
    assert schema_tokens > 0
    assert estimate_tokens(messages) + schema_tokens > estimate_tokens(messages)


def test_final_metrics_report_cache_and_schema_breakdown():
    metrics = _compute_final_metrics(
        [{"role": "user", "content": "hello"}],
        "done",
        1.0,
        0.2,
        400_000,
        real_input_tokens=1_200,
        real_output_tokens=40,
        has_real_usage=True,
        tool_events=[],
        round_texts=[],
        request_context_tokens=1_500,
        cached_input_tokens=900,
        cache_write_input_tokens=100,
        tool_schema_tokens=300,
    )

    assert metrics["cached_input_tokens"] == 900
    assert metrics["cache_write_input_tokens"] == 100
    assert metrics["uncached_input_tokens"] == 300
    assert metrics["tool_schema_tokens"] == 300
    assert metrics["request_context_tokens"] == 1_500


# ── a headless run's context window ───────────────────────────────────────


def _headless_context_length(monkeypatch, probe, session):
    """The ``context_length`` ``run_headless`` hands the agent loop."""
    import src.agent_loop as agent_loop
    import src.model_context as model_context
    from src.headless_agent import run_headless

    seen = {}

    async def fake_loop(url, model, messages, **kwargs):
        seen.update(kwargs)
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop, raising=False)
    monkeypatch.setattr(model_context, "get_context_length", probe)
    asyncio.run(run_headless(session, [{"role": "user", "content": "go"}]))
    return seen["context_length"]


def _session(**overrides):
    from core.models import Session

    sess = Session(id="w", name="worker", endpoint_url="http://llm.test/v1", model="gpt-5.6-sol")
    for key, value in overrides.items():
        setattr(sess, key, value)
    return sess


def test_session_carries_no_context_window_of_its_own():
    # The root cause of `context_length=0` on every worker: `run_headless` read
    # `getattr(sess, "context_length", 0)` off a dataclass without that field,
    # so the default fired every time and nothing ever looked wrong.
    import dataclasses

    from core.models import Session

    assert "context_length" not in {f.name for f in dataclasses.fields(Session)}


def test_headless_run_resolves_the_window_from_its_model(monkeypatch):
    # Same source a chat turn uses (chat_helpers' maybe_compact -> model_context).
    assert _headless_context_length(monkeypatch, lambda url, model: 400_000, _session()) == 400_000


def test_an_attached_window_wins_over_the_probe(monkeypatch):
    # session_tools builds a runner namespace for a profile's model and may
    # already carry the window; don't re-probe over it.
    assert _headless_context_length(
        monkeypatch, lambda url, model: 400_000, _session(context_length=32_000)) == 32_000


def test_a_failed_probe_falls_back_to_the_documented_default(monkeypatch):
    from src.model_context import DEFAULT_CONTEXT

    def _boom(url, model):
        raise RuntimeError("endpoint unreachable")

    # A stated default window beats 0: trimming and the context profile both
    # need a denominator. The *budget* is unaffected either way — the loop
    # re-derives it via budget_context_for_model, which refuses to scale off a
    # window it has not proven.
    assert _headless_context_length(monkeypatch, _boom, _session()) == DEFAULT_CONTEXT

