"""Regression tests for retrying a streaming connect failure.

Background: the stream handlers logged "transient, will retry" on a connect
error and then never retried — `stream_llm` passed the 503 straight through.
A momentary DNS blip inside the container ("[Errno -3] Temporary failure in
name resolution") therefore killed a whole agent round in 15ms, *and* burned
one of the two strikes the dead-host cooldown allows, so the non-streaming
fallback behind it raised on its own first failure with no retry either.

`stream_llm` now replays a pre-content connect failure. It is only safe for
that one error shape: the connect never landed, so no byte reached the caller
and a replay cannot duplicate streamed tokens.
"""
import asyncio
import json

import pytest

from src import llm_core
from src.llm_core import LLMConfig


@pytest.fixture(autouse=True)
def _clean_host_health():
    llm_core._clear_host_dead("http://up.test")
    yield
    llm_core._clear_host_dead("http://up.test")


def _drain(monkeypatch, attempts, *, sleep_calls=None):
    """Run stream_llm against a stubbed inner generator that yields the chunk
    list from `attempts` in order, one list per attempt."""
    seen = {"n": 0}

    async def fake_inner(url, model, messages, **kw):
        i = min(seen["n"], len(attempts) - 1)
        seen["n"] += 1
        for chunk in attempts[i]:
            yield chunk

    async def fake_sleep(delay):
        if sleep_calls is not None:
            sleep_calls.append(delay)

    monkeypatch.setattr(llm_core, "_stream_llm_inner", fake_inner)
    monkeypatch.setattr(llm_core.asyncio, "sleep", fake_sleep)

    async def run():
        return [
            c async for c in llm_core.stream_llm(
                "http://up.test/v1/chat/completions", "m", [{"role": "user", "content": "hi"}]
            )
        ]

    return asyncio.run(run()), seen


def test_connect_error_chunk_is_marked_retryable():
    chunk = llm_core._connect_error_chunk("http://up.test/v1/chat/completions")
    payload = json.loads(chunk.split("data: ", 1)[1])
    assert payload["status"] == 503
    assert payload["retryable"] is True
    assert llm_core._is_retryable_connect_chunk(chunk)


def test_other_stream_errors_are_not_replayed():
    # A 502/504 can arrive after the model already started work; replaying it
    # risks duplicate output and double-billing, so only connect errors retry.
    for status, text in ((504, "Read timeout"), (502, "Network error")):
        chunk = f'event: error\ndata: {json.dumps({"error": text, "status": status})}\n\n'
        assert not llm_core._is_retryable_connect_chunk(chunk)
    assert not llm_core._is_retryable_connect_chunk('data: {"delta": "hi"}\n\n')


def test_connect_failure_is_retried_and_succeeds(monkeypatch):
    err = llm_core._connect_error_chunk("http://up.test/v1/chat/completions")
    sleeps = []
    chunks, seen = _drain(
        monkeypatch,
        [[err], ['data: {"delta": "hello"}\n\n', "data: [DONE]\n\n"]],
        sleep_calls=sleeps,
    )
    assert seen["n"] == 2, "the failed connect should have been re-attempted"
    assert not any(c.startswith("event: error") for c in chunks)
    assert chunks[-1] == "data: [DONE]\n\n"
    assert sleeps == [LLMConfig.STREAM_CONNECT_RETRY_DELAY]


def test_retry_stops_once_the_host_is_cooled(monkeypatch):
    """A genuinely dead upstream must stay fast to fail: once the dead-host
    cooldown trips, the error goes out instead of looping the backoff."""
    err = llm_core._connect_error_chunk("http://up.test/v1/chat/completions")

    real_inner_calls = {"n": 0}

    async def fake_inner(url, model, messages, **kw):
        real_inner_calls["n"] += 1
        # Mirror what the real handlers do on a connect error.
        llm_core._mark_host_dead("http://up.test/v1/chat/completions")
        yield err

    async def _no_sleep(delay):
        return None

    monkeypatch.setattr(llm_core, "_stream_llm_inner", fake_inner)

    monkeypatch.setattr(llm_core.asyncio, "sleep", _no_sleep)

    async def run():
        return [
            c async for c in llm_core.stream_llm(
                "http://up.test/v1/chat/completions", "m", [{"role": "user", "content": "hi"}]
            )
        ]

    chunks = asyncio.run(run())
    # First failure is within the grace window (one retry); the second cools
    # the host, so the error surfaces rather than a third attempt.
    assert real_inner_calls["n"] == 2
    assert chunks[-1].startswith("event: error")


def test_error_after_content_is_never_replayed(monkeypatch):
    """Once tokens are out, a retry would duplicate them — pass the error on."""
    err = llm_core._connect_error_chunk("http://up.test/v1/chat/completions")
    chunks, seen = _drain(monkeypatch, [['data: {"delta": "partial"}\n\n', err]])
    assert seen["n"] == 1
    assert chunks[-1].startswith("event: error")
