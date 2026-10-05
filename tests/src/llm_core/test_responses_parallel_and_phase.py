"""parallel_tool_calls on the Responses payload and `phase` replay (2026-10-01)."""
import json
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import src.llm_core as llm_core
from src.chatgpt_subscription import build_responses_input
from src.llm_core import _build_chatgpt_responses_payload
from tests.src.llm_core.test_chatgpt_responses_tools import CHAT_TOOL, _collect

MSGS = [{"role": "user", "content": "hi"}]
URL = "https://chatgpt.com/backend-api/codex/responses"


@pytest.fixture(autouse=True)
def _clean_state():
    llm_core._REJECTED_REQUEST_PARAMS.clear()
    llm_core._RESPONSES_NO_PHASE_REPLAY.clear()
    yield
    llm_core._REJECTED_REQUEST_PARAMS.clear()
    llm_core._RESPONSES_NO_PHASE_REPLAY.clear()


def _build(**kw):
    return _build_chatgpt_responses_payload("gpt-6-luna", MSGS, 1.0, 0, target_url_hint=URL, **kw)


def test_parallel_tool_calls_sent_with_tools():
    assert _build(tools=[CHAT_TOOL])["parallel_tool_calls"] is True


def test_parallel_tool_calls_absent_without_tools_or_with_none():
    assert "parallel_tool_calls" not in _build()
    assert "parallel_tool_calls" not in _build(tool_choice_none=True)
    assert "parallel_tool_calls" not in _build(tools=[CHAT_TOOL], allowed_tools=[])


def test_parallel_tool_calls_stops_after_rejection():
    payload = _build(tools=[CHAT_TOOL])
    body = "Unsupported parameter: parallel_tool_calls"
    chunk = llm_core._rejected_param_retry_chunk(400, body, payload, URL, "gpt-6-luna")
    assert chunk and "retryable" in chunk
    assert "parallel_tool_calls" not in _build(tools=[CHAT_TOOL])


def test_phase_replayed_on_assistant_message_with_phase():
    items = build_responses_input([
        {"role": "assistant", "content": "checking", "responses_phase": "commentary",
         "reasoning_items": [{"type": "reasoning", "id": "rs_1"}],
         "tool_calls": [{"id": "c1", "function": {"name": "ls", "arguments": "{}"}}]},
    ])
    assert [i["type"] for i in items] == ["reasoning", "message", "function_call"]
    assert items[1] == {
        "type": "message", "role": "assistant", "phase": "commentary",
        "content": [{"type": "output_text", "text": "checking"}],
    }


def test_message_without_phase_keeps_old_shape():
    items = build_responses_input([{"role": "assistant", "content": "done"}])
    assert items == [{"role": "assistant", "content": [{"type": "output_text", "text": "done"}]}]


def test_phase_dropped_when_replay_disabled():
    items = build_responses_input(
        [{"role": "assistant", "content": "x", "responses_phase": "final_answer"}], replay_phase=False
    )
    assert items == [{"role": "assistant", "content": [{"type": "output_text", "text": "x"}]}]


def test_payload_respects_phase_rejection():
    msgs = [{"role": "assistant", "content": "x", "responses_phase": "commentary"}]
    p = _build_chatgpt_responses_payload("gpt-6-luna", msgs, 1.0, 0, target_url_hint=URL)
    assert p["input"][0]["phase"] == "commentary"
    llm_core._disable_phase_replay(URL)
    p = _build_chatgpt_responses_payload("gpt-6-luna", msgs, 1.0, 0, target_url_hint=URL)
    assert "phase" not in p["input"][0]


def test_phase_rejection_body_detection():
    assert llm_core._mentions_phase_field("Unknown parameter: 'input[2].phase'.")
    assert not llm_core._mentions_phase_field("Unsupported parameter: parallel_tool_calls")


def test_sanitize_keeps_phase_only_when_asked():
    msgs = [{"role": "assistant", "content": "x", "responses_phase": "commentary"}]
    assert llm_core._sanitize_llm_messages(msgs, keep_keys=("responses_phase",))[0]["responses_phase"] == "commentary"
    assert "responses_phase" not in llm_core._sanitize_llm_messages(msgs)[0]


async def test_stream_carries_phase_on_tool_calls_event(monkeypatch):
    chunks = await _collect(monkeypatch, [
        {"type": "response.output_item.done", "item": {"type": "message", "phase": "commentary"}},
        {"type": "response.output_item.added", "output_index": 1,
         "item": {"type": "function_call", "call_id": "c1", "name": "ls"}},
        {"type": "response.completed", "response": {}},
    ])
    events = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
    tc = [e for e in events if e.get("type") == "tool_calls"][0]
    assert tc["phase"] == "commentary"


def test_agent_loop_attaches_phase_to_tool_call_message():
    from src.agent_loop import _append_tool_results

    messages = []
    _append_tool_results(
        messages, "looking", [{"id": "c1", "name": "ls", "arguments": "{}"}],
        ["r"], ["r"], True, 1, responses_phase="commentary",
    )
    assert messages[0]["responses_phase"] == "commentary"
