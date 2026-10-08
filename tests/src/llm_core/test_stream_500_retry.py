"""An upstream 500 that arrives before any output is replayed once.

2026-10-08: three ChatGPT-backend 500s ("You can retry your request"), each the
first event of its round, ended a 74-round worker and then both attempts to
resume it. Other requests to the same host succeeded in between.
"""
import asyncio
import json

import pytest

from src import llm_core

_URL = "http://up.test/v1/chat/completions"
_ERR_500 = 'event: error\ndata: {"status": 500, "text": "An error occurred. You can retry your request"}\n\n'
_OK = ['data: {"delta": "hello"}\n\n', "data: [DONE]\n\n"]


@pytest.fixture(autouse=True)
def _clean_host_health(monkeypatch):
    llm_core._clear_host_dead(_URL)
    monkeypatch.setattr(llm_core, "_STREAM_STATUS_RETRY_DELAY", (0.0, 0.0))
    yield
    llm_core._clear_host_dead(_URL)


def _drain(monkeypatch, attempts):
    seen = {"n": 0}

    async def fake_inner(url, model, messages, **kw):
        i = min(seen["n"], len(attempts) - 1)
        seen["n"] += 1
        for chunk in attempts[i]:
            yield chunk

    monkeypatch.setattr(llm_core, "_stream_llm_inner", fake_inner)

    async def run():
        return [c async for c in llm_core.stream_llm(_URL, "m", [{"role": "user", "content": "hi"}])]

    return asyncio.run(run()), seen["n"]


def test_a_500_before_output_is_replayed(monkeypatch):
    chunks, attempts = _drain(monkeypatch, [[_ERR_500], _OK])

    assert attempts == 2
    assert chunks == _OK


def test_a_500_that_repeats_is_replayed_only_once(monkeypatch):
    chunks, attempts = _drain(monkeypatch, [[_ERR_500], [_ERR_500], _OK])

    assert attempts == 2
    assert json.loads(chunks[-1].split("data: ", 1)[1])["status"] == 500
