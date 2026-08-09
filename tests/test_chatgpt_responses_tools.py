"""Native tool calling over the ChatGPT Subscription / Codex Responses API.

This endpoint shipped with supports_tools=False, and that was accurate rather
than cautious: the payload builder emitted no `tools` key at all, and the stream
reader handled only output_text/completed/failed — so a tool call could neither
be sent nor received. Models served here (gpt-5.x) do native tool calling and do
not fall back to Odysseus's fenced-block convention, so they narrated the
attempt ("I'll update both files... the workspace tools did not run") and
stalled forever.

Three things have to line up: the request shape (flattened, not nested), the
stream events (output items + argument deltas, not delta.tool_calls), and the
round trip (a call and its result must survive back into the next request as
correlated items).
"""
import json
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

from src.chatgpt_subscription import (
    build_responses_input,
    build_responses_tools,
)


# ── request: tool schema shape ────────────────────────────────────────────

CHAT_TOOL = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": "Edit a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
}


def test_tools_are_flattened_for_responses():
    """Responses puts name/parameters on the item; the nested Chat Completions
    form is rejected."""
    out = build_responses_tools([CHAT_TOOL])
    assert out == [{
        "type": "function",
        "name": "edit_file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        "description": "Edit a file",
    }]
    assert "function" not in out[0]


def test_already_flat_tools_pass_through():
    out = build_responses_tools([{"type": "function", "name": "ls", "parameters": {}}])
    assert out[0]["name"] == "ls"


def test_tools_without_a_name_are_dropped():
    assert build_responses_tools([{"type": "function", "function": {}}]) == []
    assert build_responses_tools([None, "nonsense"]) == []


def test_missing_parameters_get_a_valid_empty_schema():
    """A bare `parameters` omission would otherwise send null and 400."""
    out = build_responses_tools([{"type": "function", "function": {"name": "ls"}}])
    assert out[0]["parameters"] == {"type": "object", "properties": {}}


def test_no_tools_means_no_tools_key():
    from src.llm_core import _build_chatgpt_responses_payload

    payload = _build_chatgpt_responses_payload("gpt-5.6", [{"role": "user", "content": "hi"}], 1.0, 0)
    assert "tools" not in payload


def test_tools_reach_the_payload():
    from src.llm_core import _build_chatgpt_responses_payload

    payload = _build_chatgpt_responses_payload(
        "gpt-5.6", [{"role": "user", "content": "hi"}], 1.0, 0, tools=[CHAT_TOOL],
    )
    assert payload["tools"][0]["name"] == "edit_file"


# ── request: the tool-call round trip ─────────────────────────────────────

def test_assistant_tool_call_becomes_a_function_call_item():
    items = build_responses_input([{
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call_abc",
            "function": {"name": "edit_file", "arguments": '{"path":"a.md"}'},
        }],
    }])
    assert items == [{
        "type": "function_call",
        "call_id": "call_abc",
        "name": "edit_file",
        "arguments": '{"path":"a.md"}',
    }]


def test_tool_result_becomes_a_correlated_output_item():
    items = build_responses_input([
        {"role": "tool", "tool_call_id": "call_abc", "content": "wrote 1 line"},
    ])
    assert items == [{
        "type": "function_call_output",
        "call_id": "call_abc",
        "output": "wrote 1 line",
    }]


def test_call_and_result_stay_correlated():
    """The whole point: the model must see its own call AND the result keyed by
    the same id, or it re-plans the same call every round."""
    items = build_responses_input([
        {"role": "user", "content": "add SCANPROOF to a.md"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "function": {"name": "edit_file", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ])
    call = next(i for i in items if i.get("type") == "function_call")
    result = next(i for i in items if i.get("type") == "function_call_output")
    assert call["call_id"] == result["call_id"] == "call_1"


def test_assistant_prose_and_call_both_survive():
    items = build_responses_input([{
        "role": "assistant",
        "content": "I'll edit it.",
        "tool_calls": [{"id": "c1", "function": {"name": "edit_file", "arguments": "{}"}}],
    }])
    assert [i.get("type") or "message" for i in items] == ["message", "function_call"]


def test_dict_arguments_are_serialised():
    """_normalize_messages_for_provider decodes arguments to dicts for some
    providers; Responses wants the JSON string."""
    items = build_responses_input([{
        "role": "assistant",
        "tool_calls": [{"id": "c1", "function": {"name": "ls", "arguments": {"path": "/tmp"}}}],
    }])
    assert json.loads(items[0]["arguments"]) == {"path": "/tmp"}


def test_tool_message_without_an_id_degrades_to_text():
    """Emitting a function_call_output with no call_id would be rejected."""
    items = build_responses_input([{"role": "tool", "content": "orphan"}])
    assert items[0]["role"] == "user"
    assert items[0]["content"][0]["text"] == "orphan"


def test_plain_messages_are_unchanged():
    items = build_responses_input([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ])
    assert items == [
        {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "hi"}]},
    ]


# ── response: stream event parsing ────────────────────────────────────────

def _sse(events):
    return [f"data: {json.dumps(e)}" for e in events]


async def _collect(monkeypatch, events):
    """Drive the real chatgpt-subscription stream branch over fake SSE lines."""
    import src.llm_core as llm_core

    class _Resp:
        status_code = 200

        async def aiter_lines(self):
            for line in _sse(events):
                yield line

        async def aread(self):
            return b""

    class _Stream:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *a):
            return False

    class _Client:
        def stream(self, *a, **k):
            return _Stream()

    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _Client())
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda url: None)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)

    out = []
    async for chunk in llm_core.stream_llm(
        "https://chatgpt.com/backend-api/codex",
        "gpt-5.6-sol",
        [{"role": "user", "content": "edit a.md"}],
    ):
        out.append(chunk)
    return out


def _tool_calls_from(chunks):
    for chunk in chunks:
        if not chunk.startswith("data: "):
            continue
        body = chunk[6:].strip()
        if body == "[DONE]":
            continue
        try:
            payload = json.loads(body)
        except ValueError:
            continue
        if payload.get("type") == "tool_calls":
            return payload["calls"]
    return None


async def test_streamed_function_call_is_emitted(monkeypatch):
    calls = _tool_calls_from(await _collect(monkeypatch, [
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "function_call", "call_id": "call_1", "name": "edit_file"}},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"path":'},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '"a.md"}'},
        {"type": "response.completed", "response": {}},
    ]))
    assert calls == [{"id": "call_1", "name": "edit_file", "arguments": '{"path":"a.md"}'}]


async def test_arguments_done_is_authoritative(monkeypatch):
    """A dropped delta must not leave truncated JSON."""
    calls = _tool_calls_from(await _collect(monkeypatch, [
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "function_call", "call_id": "c1", "name": "ls"}},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"pa'},
        {"type": "response.function_call_arguments.done", "output_index": 0,
         "arguments": '{"path":"/tmp"}'},
        {"type": "response.completed", "response": {}},
    ]))
    assert json.loads(calls[0]["arguments"]) == {"path": "/tmp"}


async def test_parallel_calls_stay_separate(monkeypatch):
    calls = _tool_calls_from(await _collect(monkeypatch, [
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "function_call", "call_id": "c1", "name": "read_file"}},
        {"type": "response.output_item.added", "output_index": 1,
         "item": {"type": "function_call", "call_id": "c2", "name": "edit_file"}},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"p":1}'},
        {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '{"p":2}'},
        {"type": "response.completed", "response": {}},
    ]))
    assert [c["name"] for c in calls] == ["read_file", "edit_file"]
    assert [c["arguments"] for c in calls] == ['{"p":1}', '{"p":2}']


async def test_text_only_response_emits_no_tool_calls(monkeypatch):
    chunks = await _collect(monkeypatch, [
        {"type": "response.output_text.delta", "delta": "hello"},
        {"type": "response.completed", "response": {}},
    ])
    assert _tool_calls_from(chunks) is None
    assert any('"delta": "hello"' in c for c in chunks)


async def test_calls_survive_a_stream_that_never_completes(monkeypatch):
    """Truncated stream must still surface the call, not silently drop it."""
    calls = _tool_calls_from(await _collect(monkeypatch, [
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "function_call", "call_id": "c1", "name": "ls"}},
        {"type": "response.function_call_arguments.done", "output_index": 0, "arguments": "{}"},
    ]))
    assert calls == [{"id": "c1", "name": "ls", "arguments": "{}"}]


# ── classification ────────────────────────────────────────────────────────

def test_codex_host_classifies_as_an_api_model():
    """Otherwise the agent sends no schemas and waits for prose tool blocks that
    these models never emit."""
    from src.agent_loop import _API_HOSTS

    assert any(h in "https://chatgpt.com/backend-api/codex/responses" for h in _API_HOSTS)
