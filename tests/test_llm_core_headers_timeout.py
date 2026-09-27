"""A streaming request whose response headers never arrive.

Regression for the 2026-09-27 Codex worker: round 27 was sent to
chatgpt.com/backend-api/codex/responses, no `POST ... 200 OK` ever came back,
and the worker sat silent for the whole 300s read timeout before failing, while
other chats' requests to the same host were answered in seconds. The headers
wait is now bounded separately, logged, and replayed once.
"""
import asyncio
import json
import logging

import httpx
import pytest

from src import llm_core


CODEX_URL = "https://chatgpt.com/backend-api/codex"


def _completed_sse() -> bytes:
    events = [
        {"type": "response.output_text.delta", "delta": "hello"},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 3, "output_tokens": 1}}},
    ]
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


def _hanging_then_ok(calls, hang_count):
    """Transport handler: the first `hang_count` requests never get headers."""

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) <= hang_count:
            trace = request.extensions.get("trace")
            if trace is not None:
                # What httpcore reports once the body is out and it is reading
                # the status line: the server has the request and is sitting on it.
                await trace("http11.send_request_body.complete", {})
                await trace("http11.receive_response_headers.started", {})
            await asyncio.Event().wait()  # never answers
        return httpx.Response(200, content=_completed_sse(),
                              headers={"content-type": "text/event-stream"})

    return handler


@pytest.fixture
def mocked(monkeypatch):
    state = {"calls": []}

    def install(hang_count, headers_timeout=0.2):
        client = httpx.AsyncClient(transport=httpx.MockTransport(_hanging_then_ok(state["calls"], hang_count)))
        state["client"] = client
        monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
        monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)
        monkeypatch.setattr(llm_core, "_clear_host_dead", lambda url: None)
        monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
        monkeypatch.setattr(
            llm_core, "_stream_headers_timeout",
            lambda url, read_timeout: headers_timeout,
        )
        return state

    return install


async def _collect(url=CODEX_URL, timeout=300):
    out = []
    async for chunk in llm_core.stream_llm(url, "gpt-6-luna", [{"role": "user", "content": "hi"}],
                                           timeout=timeout):
        out.append(chunk)
    return out


def _errors(chunks):
    errs = []
    for chunk in chunks:
        if chunk.startswith("event: error"):
            for line in chunk.split("\n"):
                if line.startswith("data: "):
                    errs.append(json.loads(line[6:]))
    return errs


def _text(chunks):
    text = ""
    for chunk in chunks:
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            payload = json.loads(chunk[6:])
            text += payload.get("delta") or ""
    return text


async def test_hung_request_is_replayed_once_and_then_answers(mocked, caplog):
    state = mocked(hang_count=1)
    caplog.set_level(logging.INFO, logger="src.llm_core")

    loop = asyncio.get_running_loop()
    started = loop.time()
    chunks = await asyncio.wait_for(_collect(), timeout=10)
    elapsed = loop.time() - started

    assert len(state["calls"]) == 2
    assert _text(chunks) == "hello"
    assert not _errors(chunks), "a replayed request that answered must not surface the first attempt's error"
    assert elapsed < 5, "the headers deadline, not the 300s read timeout, must end the hung attempt"
    messages = [r.getMessage() for r in caplog.records]
    assert any("no response headers after 0.2s from https://chatgpt.com" in m and "stage=waiting_for_headers" in m
               for m in messages), messages
    assert any("retrying https://chatgpt.com model=gpt-6-luna after no response headers (attempt 2/2)" in m
               for m in messages), messages
    await state["client"].aclose()


async def test_request_that_never_answers_fails_with_504_after_one_retry(mocked, caplog):
    state = mocked(hang_count=99)
    caplog.set_level(logging.INFO, logger="src.llm_core")

    chunks = await asyncio.wait_for(_collect(), timeout=10)

    assert len(state["calls"]) == 2, "exactly one replay"
    errors = _errors(chunks)
    assert len(errors) == 1
    assert errors[0]["status"] == 504
    assert errors[0]["headers_timeout"] is True
    assert "No response from https://chatgpt.com after 0.2s" in errors[0]["error"]
    assert not errors[0].get("retryable"), "the connect-retry path must not replay it again"
    assert any("giving up" in r.getMessage() for r in caplog.records)
    await state["client"].aclose()


async def test_openai_compatible_path_is_bounded_too(mocked):
    state = mocked(hang_count=99)
    chunks = await asyncio.wait_for(_collect(url="https://api.openai.com/v1"), timeout=10)
    assert len(state["calls"]) == 2
    assert _errors(chunks)[0]["status"] == 504
    await state["client"].aclose()


def test_headers_timeout_policy(monkeypatch):
    import src.settings as settings

    values = {}
    monkeypatch.setattr(settings, "get_setting",
                        lambda key, default=None: values.get(key, default))

    # Default: bounded for a cloud endpoint, shorter than the read timeout.
    assert llm_core._stream_headers_timeout(CODEX_URL + "/responses", 300) == 120.0
    # Never for a local endpoint: a model load holds the headers legitimately.
    assert llm_core._stream_headers_timeout("http://localhost:11434/api/chat", 300) is None
    # Not when it would not be shorter than the read timeout.
    assert llm_core._stream_headers_timeout(CODEX_URL, 60) is None
    # 0 switches it off; junk falls back to the default.
    values["agent_stream_headers_timeout_seconds"] = 0
    assert llm_core._stream_headers_timeout(CODEX_URL, 300) is None
    values["agent_stream_headers_timeout_seconds"] = "junk"
    assert llm_core._stream_headers_timeout(CODEX_URL, 300) == 120.0
    values["agent_stream_headers_timeout_seconds"] = 45
    assert llm_core._stream_headers_timeout(CODEX_URL, 300) == 45.0


def test_stage_names():
    assert llm_core._headers_stage("") == "acquiring_connection"
    assert llm_core._headers_stage("connection.connect_tcp.started") == "connecting"
    assert llm_core._headers_stage("http11.send_request_body.started") == "sending_request"
    assert llm_core._headers_stage("http11.send_request_body.complete") == "waiting_for_headers"
    assert llm_core._headers_stage("http11.receive_response_headers.started") == "waiting_for_headers"


async def test_slow_body_after_headers_is_not_cut_by_headers_deadline(monkeypatch):
    """The deadline ends at the headers; a body slower than it still streams."""

    async def handler(request):
        async def body():
            await asyncio.sleep(0.4)  # longer than the 0.2s headers budget
            yield _completed_sse()

        return httpx.Response(200, content=body(), headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda url: None)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_stream_headers_timeout", lambda url, rt: 0.2)

    chunks = await asyncio.wait_for(_collect(), timeout=10)
    assert _text(chunks) == "hello"
    assert not _errors(chunks)
    await client.aclose()
