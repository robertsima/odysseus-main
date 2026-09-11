"""A model that refuses a request field must not take the whole call down.

Seen live: `ask_teacher` on `gpt-6-astra` (ChatGPT Subscription / Codex
Responses) got HTTP 400 "Unsupported parameter: temperature" because the
reasoning-model table only knew `gpt-5`, and the agent gave up on the teacher.
Two layers fix it: the table is now version-aware (gpt-5 and every later
generation, plus the o-series), and any endpoint that still rejects a field is
answered by dropping that field, replaying the request once, and omitting the
field for that (host, model) from then on.
"""
import json

import httpx
import pytest

from src import llm_core


@pytest.fixture(autouse=True)
def _clean_state():
    llm_core._REJECTED_REQUEST_PARAMS.clear()
    llm_core._response_cache.clear()
    yield
    llm_core._REJECTED_REQUEST_PARAMS.clear()
    llm_core._response_cache.clear()


# ── static table: newer generations are reasoning models too ─────────────────

@pytest.mark.parametrize(
    "model",
    ["gpt-6-astra", "gpt-5.6-sol", "gpt-5-codex", "GPT-6", "openai/gpt-7-preview",
     "o5-preview", "openrouter/openai/o4-mini"],
)
def test_newer_openai_generations_restrict_temperature(model):
    assert llm_core._restricts_temperature(model) is True


@pytest.mark.parametrize(
    "model",
    ["gpt-4.5-preview", "gpt-oss-120b", "gpt-4o", "llama-3.1-70b", "kimi-k2",
     "octopus-4-8", "foo-o1"],
)
def test_non_reasoning_models_keep_temperature(model):
    assert llm_core._restricts_temperature(model) is False


@pytest.mark.parametrize("model,expected", [
    ("gpt-6-astra", True),
    ("gpt-5.6-sol", True),
    ("gpt-4.5-preview", True),
    ("o3-mini", True),
    ("gpt-4o", False),
    ("gpt-oss-120b", False),
    ("qwen3-32b", False),
])
def test_max_completion_tokens_is_version_aware(model, expected):
    assert llm_core._uses_max_completion_tokens(model) is expected


# ── recognising a field rejection ────────────────────────────────────────────

@pytest.mark.parametrize("status,body,payload,expected", [
    # Codex Responses backend, the live failure
    (400, '{"detail": "Unsupported parameter: temperature"}',
     {"model": "x", "temperature": 1.0}, "temperature"),
    # OpenAI chat completions on a reasoning model
    (400, "Unsupported parameter: 'temperature' is not supported with this model.",
     {"model": "x", "temperature": 0.3}, "temperature"),
    (400, "temperature does not support 0.3 with this model. Only the default (1) value is supported.",
     {"model": "x", "temperature": 0.3}, "temperature"),
    # Older OpenAI wording
    (400, '{"error": {"message": "Unrecognized request argument supplied: top_p"}}',
     {"model": "x", "temperature": 1.0, "top_p": 0.9}, "top_p"),
    # Anthropic (Opus 4.7+ dropped the sampling fields)
    (400, '{"type":"error","error":{"type":"invalid_request_error","message":"temperature: Extra inputs are not permitted"}}',
     {"model": "x", "temperature": 0.2}, "temperature"),
    # pydantic-based local servers answer 422
    (422, "[{'type': 'extra_forbidden', 'loc': ('body', 'think'), 'msg': 'Extra inputs are not permitted'}]",
     {"model": "x", "think": False}, "think"),
    # Not a schema rejection → leave the payload alone
    (429, "Unsupported parameter: temperature", {"temperature": 1.0}, None),
    (400, '{"error": {"message": "Rate limit reached"}}', {"temperature": 1.0}, None),
    (400, "This model's maximum context length is 128000 tokens.", {"max_tokens": 10}, None),
    # Names a field whose absence would change the request's meaning
    (400, "Unsupported parameter: messages", {"messages": [], "temperature": 1.0}, None),
    # Names a field we did not send
    (400, "Unsupported parameter: temperature", {"model": "x"}, None),
])
def test_rejected_request_param_detection(status, body, payload, expected):
    assert llm_core._rejected_request_param(status, body, payload) == expected


def test_rejection_is_remembered_per_host_and_model():
    url = "https://chatgpt.com/backend-api/codex/responses"
    payload = {"model": "codex-astra", "temperature": 1.0, "input": []}
    assert llm_core._retry_without_rejected_param(
        400, "Unsupported parameter: temperature", payload, url, "codex-astra",
    ) == "temperature"
    assert "temperature" not in payload

    fresh = llm_core._strip_rejected_params(
        {"model": "codex-astra", "temperature": 0.5}, url, "codex-astra",
    )
    assert "temperature" not in fresh, "later payloads for the same model omit the field up front"

    other = llm_core._strip_rejected_params(
        {"model": "gpt-4o", "temperature": 0.5}, url, "gpt-4o",
    )
    assert other["temperature"] == 0.5, "a different model on the same host is untouched"


def test_max_tokens_rejection_moves_the_value_across():
    url = "https://api.openai.com/v1/chat/completions"
    body = ("Unsupported parameter: 'max_tokens' is not supported with this model. "
            "Use 'max_completion_tokens' instead.")
    payload = {"model": "gpt-9", "max_tokens": 5}
    assert llm_core._retry_without_rejected_param(400, body, payload, url, "gpt-9") == "max_tokens"
    assert payload == {"model": "gpt-9", "max_completion_tokens": 5}
    assert llm_core._strip_rejected_params({"max_tokens": 7}, url, "gpt-9") == {"max_completion_tokens": 7}


# ── the live paths replay without the field ─────────────────────────────────

def _codex_sse(text):
    return [
        "data: " + json.dumps({"type": "response.output_text.delta", "delta": text}),
        "data: " + json.dumps({"type": "response.completed", "response": {"usage": {}}}),
    ]


async def test_codex_stream_replays_without_the_rejected_field(monkeypatch):
    """First attempt carries `temperature` (the model is unknown to the table),
    the backend refuses it, the replay omits it, and the caller sees only the
    successful stream — no error chunk, no duplicated output."""
    statuses = [400, 200]
    payloads = []

    class _Resp:
        def __init__(self, status):
            self.status_code = status

        async def aiter_lines(self):
            for line in _codex_sse("hi"):
                yield line

        async def aread(self):
            return b'{"detail": "Unsupported parameter: temperature"}'

    class _Stream:
        def __init__(self, *a, **k):
            payloads.append(k.get("json"))

        async def __aenter__(self):
            return _Resp(statuses.pop(0))

        async def __aexit__(self, *a):
            return False

    class _Client:
        def stream(self, *a, **k):
            return _Stream(*a, **k)

    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _Client())
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda url: None)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "get_context_length", lambda *a, **k: 128000)
    monkeypatch.setattr(llm_core.LLMConfig, "STREAM_CONNECT_RETRY_DELAY", 0)

    out = []
    async for chunk in llm_core.stream_llm(
        "https://chatgpt.com/backend-api/codex",
        "codex-astra-experimental",
        [{"role": "user", "content": "hello"}],
        temperature=0.7,
    ):
        out.append(chunk)

    assert [p.get("temperature") for p in payloads] == [0.7, None]
    assert not any(c.startswith("event: error") for c in out), out
    deltas = [json.loads(c[6:]).get("delta") for c in out
              if c.startswith("data: ") and c[6:].strip() != "[DONE]"]
    assert deltas.count("hi") == 1

    # A later request to the same model never sends the field again.
    statuses[:] = [200]
    payloads.clear()
    async for _ in llm_core.stream_llm(
        "https://chatgpt.com/backend-api/codex",
        "codex-astra-experimental",
        [{"role": "user", "content": "again"}],
        temperature=0.7,
    ):
        pass
    assert payloads and "temperature" not in payloads[0]


def test_sync_call_replays_without_the_rejected_field(monkeypatch):
    posted = []
    responses = [
        (400, {"error": {"message": "Unsupported parameter: 'temperature' is not supported with this model."}}),
        (200, {"choices": [{"message": {"content": "OK"}}]}),
        (200, {"choices": [{"message": {"content": "OK"}}]}),
    ]

    def fake_post(url, headers=None, json=None, timeout=None):
        posted.append(dict(json))
        status, body = responses.pop(0)
        return httpx.Response(status, request=httpx.Request("POST", url), json=body)

    monkeypatch.setattr(llm_core.httpx, "post", fake_post)
    url = "https://api.example.com/v1/chat/completions"

    assert llm_core.llm_call(url, "mystery-9", [{"role": "user", "content": "a"}], temperature=0.2) == "OK"
    assert "temperature" in posted[0]
    assert "temperature" not in posted[1]

    assert llm_core.llm_call(url, "mystery-9", [{"role": "user", "content": "b"}], temperature=0.2) == "OK"
    assert len(posted) == 3 and "temperature" not in posted[2]


async def test_async_call_replays_without_the_rejected_field(monkeypatch):
    posted = []
    responses = [
        (400, {"error": {"message": "Unrecognized request argument supplied: temperature"}}),
        (200, {"choices": [{"message": {"content": "OK"}}]}),
    ]

    async def fake_post(client, url, headers, json=None, timeout=None):
        posted.append(dict(json))
        status, body = responses.pop(0)
        return httpx.Response(status, request=httpx.Request("POST", url), json=body)

    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware_async", fake_post)
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: object())
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda url: None)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)

    result = await llm_core.llm_call_async(
        "https://api.example.com/v1/chat/completions", "mystery-10",
        [{"role": "user", "content": "a"}], temperature=0.2, max_retries=1,
    )
    assert result == "OK"
    assert [("temperature" in p) for p in posted] == [True, False], (
        "the replay must not count against max_retries"
    )
