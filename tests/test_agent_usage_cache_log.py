"""Each agent round logs how much of its prompt the provider served from cache.

The provider reports cached input tokens (llm_core normalizes every path to
`cached_input_tokens`), but the loop dropped them, so a turn that re-prefilled
its whole prompt every round looked the same in the logs as one that hit.
"""
import asyncio
import json
import logging

import src.agent_loop as agent_loop


def _collect(gen):
    async def _run():
        return [chunk async for chunk in gen]
    return asyncio.run(_run())


def test_round_logs_cached_input_tokens(monkeypatch, caplog):
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "_classify_agent_request", lambda messages, latest: {
        "low_signal": False, "continuation": False, "domains": [], "retrieval_query": latest})

    async def fake_stream(candidates, messages, **kwargs):
        yield "data: " + json.dumps({"type": "usage", "data": {
            "input_tokens": 12000, "output_tokens": 40, "cached_input_tokens": 9000}}) + "\n\n"
        yield 'data: {"delta": "Here is the answer."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    with caplog.at_level(logging.INFO, logger="src.agent_loop"):
        _collect(agent_loop.stream_agent_loop(
            "https://x.example/v1", "generic-model",
            [{"role": "user", "content": "Summarize the design of the scheduler in detail please"}],
            relevant_tools=set(), _is_teacher_run=True,
        ))
    lines = [r.getMessage() for r in caplog.records if "[agent-usage]" in r.getMessage()]
    assert lines, "no [agent-usage] line logged"
    assert "input=12000 cached=9000 (75%) output=40" in lines[0]
