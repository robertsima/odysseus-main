"""A connection that drops or goes silent before any output is replayed once.

2026-10-07/08: workers on the ChatGPT backend died on "502 Upstream protocol
error" (httpx ProtocolError), and a read timeout became a 504 that was never
replayed either. Neither logged the exception, so a stale pooled connection
could not be told from a stream cut mid-answer. A local connection-pool
timeout is also a 504 and must keep falling through to the next route at once.
"""
import asyncio
import json
import logging

import httpx
import pytest

from src import llm_core

CODEX_URL = "https://chatgpt.com/backend-api/codex"
OPENAI_URL = "https://api.openai.com/v1"


def _codex_body() -> bytes:
    events = [
        {"type": "response.output_text.delta", "delta": "hello"},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 3, "output_tokens": 1}}},
    ]
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


def _openai_body() -> bytes:
    return (b'data: {"choices": [{"delta": {"content": "hello"}}]}\n\n'
            b"data: [DONE]\n\n")


ROUTES = [(CODEX_URL, _codex_body), (OPENAI_URL, _openai_body)]


@pytest.fixture
def upstream(monkeypatch):
    """Install a transport whose first `fail_count` requests raise `exc`."""
    monkeypatch.setattr(llm_core, "_STREAM_STATUS_RETRY_DELAY", (0.0, 0.0))
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda url: None)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_stream_headers_timeout", lambda url, rt: None)
    state = {"calls": 0}

    def install(exc_type, fail_count, body, message="boom"):
        async def handler(request):
            state["calls"] += 1
            if state["calls"] <= fail_count:
                raise exc_type(message, request=request)
            return httpx.Response(200, content=body(), headers={"content-type": "text/event-stream"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
        return state

    return install


async def _collect(url):
    return [c async for c in llm_core.stream_llm(url, "gpt-6-luna", [{"role": "user", "content": "hi"}])]


def _errors(chunks):
    return [json.loads(c.split("data: ", 1)[1]) for c in chunks if c.startswith("event: error")]


def _text(chunks):
    out = ""
    for c in chunks:
        if c.startswith("data: ") and "[DONE]" not in c:
            out += json.loads(c[6:]).get("delta") or ""
    return out


@pytest.mark.parametrize("url,body", ROUTES)
async def test_protocol_error_before_output_is_replayed_and_logged(upstream, caplog, url, body):
    caplog.set_level(logging.WARNING, logger="src.llm_core")
    state = upstream(httpx.RemoteProtocolError, 1, body,
                     "Server disconnected without sending a response.")

    chunks = await asyncio.wait_for(_collect(url), timeout=10)

    assert state["calls"] == 2
    assert _text(chunks) == "hello"
    assert not _errors(chunks)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "RemoteProtocolError: Server disconnected without sending a response." in logged


@pytest.mark.parametrize("url,body", ROUTES)
async def test_read_timeout_before_output_is_replayed(upstream, url, body):
    state = upstream(httpx.ReadTimeout, 1, body)

    chunks = await asyncio.wait_for(_collect(url), timeout=10)

    assert state["calls"] == 2
    assert _text(chunks) == "hello"


async def test_a_drop_that_repeats_is_replayed_only_once(upstream):
    state = upstream(httpx.RemoteProtocolError, 99, _codex_body)

    chunks = await asyncio.wait_for(_collect(CODEX_URL), timeout=10)

    assert state["calls"] == 2
    errors = _errors(chunks)
    assert len(errors) == 1 and errors[0]["status"] == 502
    assert errors[0]["fallback_eligible"] is False


async def test_local_pool_timeout_is_not_replayed(upstream):
    # The same 504 status as a read timeout, but nothing reached the upstream:
    # it must go straight to the caller (and its next route), not replay here.
    state = upstream(httpx.PoolTimeout, 99, _openai_body)

    chunks = await asyncio.wait_for(_collect(OPENAI_URL), timeout=10)

    assert state["calls"] == 1
    assert _errors(chunks)[0]["status"] == 504


async def test_a_drop_after_output_is_never_replayed(monkeypatch, upstream):
    upstream(httpx.RemoteProtocolError, 0, _openai_body)
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1

        async def body():
            yield b'data: {"choices": [{"delta": {"content": "partial"}}]}\n\n'
            raise httpx.RemoteProtocolError("peer closed connection", request=request)

        return httpx.Response(200, content=body(), headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)

    chunks = await asyncio.wait_for(_collect(OPENAI_URL), timeout=10)

    assert calls["n"] == 1
    assert _text(chunks) == "partial"
    assert _errors(chunks)[0]["status"] == 502
