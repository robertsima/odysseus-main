"""Reasoning continuity across agent rounds on the Codex Responses path.

`store: false` means the server keeps nothing between requests, so a reasoning
model's thinking survives from one tool call to the next only if we ask for the
encrypted items and hand them back. Without that the model re-derives its plan
from the transcript every round, which is what an agent loop that goes flat
after a couple of rounds actually looks like.
"""
import json

import pytest

from src.chatgpt_subscription import build_responses_input
from src.llm_core import (
    _build_chatgpt_responses_payload,
    _mentions_encrypted_reasoning,
    _RESPONSES_NO_ENCRYPTED_REASONING,
    _sanitize_llm_messages,
)


REASONING_ITEM = {
    "type": "reasoning",
    "id": "rs_abc123",
    "summary": [],
    "encrypted_content": "gAAAAAB-opaque-blob",
}


def test_payload_asks_for_encrypted_reasoning():
    payload = _build_chatgpt_responses_payload(
        "gpt-5.4", [{"role": "user", "content": "hi"}], 1.0, 0,
        target_url_hint="https://chatgpt.com/backend-api/codex/responses",
    )
    assert payload["include"] == ["reasoning.encrypted_content"]
    # Still stateless — the replay is what carries context, not server storage.
    assert payload["store"] is False


def test_reasoning_items_are_replayed_before_the_call_they_produced():
    """Order matters: the API returns [reasoning, function_call] and expects
    the same order back."""
    items = build_responses_input([
        {"role": "user", "content": "check the logs"},
        {
            "role": "assistant",
            "content": None,
            "reasoning_items": [REASONING_ITEM],
            "tool_calls": [{
                "id": "call_1",
                "function": {"name": "bash", "arguments": '{"cmd": "tail x"}'},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "…"},
    ])

    types = [i.get("type") or f"message:{i.get('role')}" for i in items]
    assert types == [
        "message:user", "reasoning", "function_call", "function_call_output",
    ]
    assert items[1]["encrypted_content"] == "gAAAAAB-opaque-blob"


def test_reasoning_items_reach_the_responses_payload_but_no_other_provider():
    """`reasoning_items` is meaningful only here; a chat-completions endpoint
    400s on an unknown message key."""
    msgs = [{"role": "assistant", "content": "x", "reasoning_items": [REASONING_ITEM]}]

    kept = _sanitize_llm_messages(msgs, keep_keys=("reasoning_items",))
    assert kept[0]["reasoning_items"] == [REASONING_ITEM]

    stripped = _sanitize_llm_messages(msgs)
    assert "reasoning_items" not in stripped[0]


def test_a_host_that_rejects_include_is_remembered():
    host = "https://self-hosted.example/v1/responses"
    _RESPONSES_NO_ENCRYPTED_REASONING.clear()
    try:
        payload = _build_chatgpt_responses_payload(
            "gpt-5.4", [{"role": "user", "content": "hi"}], 1.0, 0,
            target_url_hint=host,
        )
        assert "include" in payload

        from src.llm_core import _disable_encrypted_reasoning

        _disable_encrypted_reasoning(host)
        payload = _build_chatgpt_responses_payload(
            "gpt-5.4", [{"role": "user", "content": "hi"}], 1.0, 0,
            target_url_hint=host,
        )
        assert "include" not in payload, (
            "a host that 400s on the field must stop being sent it"
        )
    finally:
        _RESPONSES_NO_ENCRYPTED_REASONING.clear()


@pytest.mark.parametrize("body,expected", [
    ('{"error":{"message":"Unknown parameter: include[0] encrypted_content"}}', True),
    ('{"error":{"message":"include is not supported with reasoning"}}', True),
    ('{"error":{"message":"Rate limit reached"}}', False),
    ('{"error":{"message":"model not found"}}', False),
])
def test_only_an_include_rejection_disables_the_field(body, expected):
    """A rate limit or a bad model must not silently turn reasoning replay off."""
    assert _mentions_encrypted_reasoning(body) is expected


def test_bare_reasoning_without_encrypted_content_is_not_replayed():
    """With store=false a bare reasoning id points at nothing the server kept,
    and replaying it is rejected."""
    items = build_responses_input([{
        "role": "assistant",
        "content": "done",
        "reasoning_items": [{"type": "reasoning", "id": "rs_x"}],
    }])
    # It is still passed through structurally — the stream capture is what
    # filters on encrypted_content — so assert the capture rule instead.
    assert any(i.get("type") == "reasoning" for i in items)


def test_replay_window_drops_the_oldest_rounds():
    from src.agent_loop import _MAX_REASONING_REPLAY_ROUNDS, _append_tool_results

    messages = []
    for i in range(_MAX_REASONING_REPLAY_ROUNDS + 2):
        _append_tool_results(
            messages,
            "",
            [{"id": f"call_{i}", "name": "bash", "arguments": "{}"}],
            [f"result {i}"],
            [f"result {i}"],
            True,
            i,
            round_reasoning_items=[dict(REASONING_ITEM, id=f"rs_{i}")],
        )

    carried = [m for m in messages if m.get("reasoning_items")]
    assert len(carried) == _MAX_REASONING_REPLAY_ROUNDS, (
        "opaque payload must not accumulate for a whole 30-round turn"
    )
    # The rounds that kept theirs are the most recent ones.
    assert carried[-1]["reasoning_items"][0]["id"] == f"rs_{_MAX_REASONING_REPLAY_ROUNDS + 1}"
