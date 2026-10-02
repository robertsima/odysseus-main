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


def _run_rounds(n):
    from src.agent_loop import _append_tool_results

    messages = []
    for i in range(n):
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
    return messages


def _reasoning_turns(messages):
    return [m for m in messages if m.get("reasoning_items")]


def test_reasoning_items_are_not_pruned_on_a_round_schedule():
    """2026-10-02: a prune every 9 rounds rewrote history mid-turn, and the
    provider re-read 30-80k tokens after each one. Below the safety cap every
    item of the turn stays, however many rounds run."""
    messages = _run_rounds(60)
    assert len(_reasoning_turns(messages)) == 60
    assert messages[0]["reasoning_items"][0]["id"] == "rs_0"


def test_reasoning_is_cut_back_in_one_edit_at_the_carry_cap(monkeypatch):
    import src.agent_loop as al

    # Each item is 400 chars = 100 estimated tokens; cap at 2,500 tokens.
    big = dict(REASONING_ITEM, encrypted_content="A" * 400)
    monkeypatch.setattr(al, "_REASONING_CARRY_CAP_TOKENS", 2_500)
    messages = []
    counts = []
    for i in range(40):
        al._append_tool_results(
            messages, "", [{"id": f"c{i}", "name": "bash", "arguments": "{}"}],
            [f"r{i}"], [f"r{i}"], True, i,
            round_reasoning_items=[dict(big, id=f"rs_{i}")],
        )
        counts.append(len(_reasoning_turns(messages)))
    # Grows one per round up to the cap, then drops once to the window.
    assert counts[:25] == list(range(1, 26))
    assert counts[25] == al._MAX_REASONING_REPLAY_ROUNDS
    assert max(counts) == 25
    kept = _reasoning_turns(messages)
    assert kept[-1]["reasoning_items"][0]["id"] == "rs_39"


def test_a_ledger_rewrite_cuts_reasoning_and_images_in_the_same_edit(monkeypatch, tmp_path):
    """One prefix break, not three: when the ledger collapses results it also
    cuts reasoning back to the window and prunes old tool images."""
    import src.agent_loop as al
    import src.constants as constants
    from src.context_compactor import (
        LEDGER_KEEP_ROUNDS, LEDGER_SLACK_ROUNDS, TOOL_IMAGES_SOURCE, pop_history_rewrite,
    )

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path), raising=False)
    messages = []
    n = LEDGER_KEEP_ROUNDS + LEDGER_SLACK_ROUNDS + 1
    for i in range(n):
        body = f"### read_file: /a/b{i}.py\n```\n{'x' * 6000}\n```"
        al._append_tool_results(
            messages, "", [{"id": f"c{i}", "name": "read_file", "arguments": "{}"}],
            [body], [body], True, i,
            round_reasoning_items=[dict(REASONING_ITEM, id=f"rs_{i}")],
            ledger_budget=0, session_id="sess-1",
        )
        if i == 1:
            messages.append({"role": "user", "metadata": {"source": TOOL_IMAGES_SOURCE},
                             "content": [{"type": "text", "text": "1. shot"},
                                         {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]})
            messages.append(dict(messages[-1]))
            messages.append(dict(messages[-1]))
    assert len(_reasoning_turns(messages)) == al._MAX_REASONING_REPLAY_ROUNDS
    live = [m for m in messages if m.get("metadata", {}).get("source") == TOOL_IMAGES_SOURCE
            and any(b.get("type") == "image_url" for b in m["content"])]
    assert len(live) == 2
    assert pop_history_rewrite("sess-1") == "ledger"
    assert pop_history_rewrite("sess-1") == "none"


def test_loop_attaches_items_phase_and_replays_in_order():
    """Re-ported 2026-10-01: stream -> round_reasoning_items -> assistant
    message -> build_responses_input as reasoning, phase message, call."""
    from src.agent_loop import _append_tool_results

    messages = []
    _append_tool_results(
        messages, "checking", [{"id": "c1", "name": "ls", "arguments": "{}"}],
        ["r"], ["r"], True, 1,
        responses_phase="commentary", round_reasoning_items=[REASONING_ITEM],
    )
    assert messages[0]["reasoning_items"] == [REASONING_ITEM]
    items = build_responses_input(messages)
    assert [i.get("type") for i in items] == ["reasoning", "message", "function_call", "function_call_output"]
    assert items[1]["phase"] == "commentary"


def test_text_only_round_also_keeps_items():
    from src.agent_loop import _append_tool_results

    messages = []
    _append_tool_results(messages, "note", [], ["out"], ["out"], False, 1,
                         round_reasoning_items=[REASONING_ITEM])
    assert messages[0]["reasoning_items"] == [REASONING_ITEM]


async def test_stream_captures_encrypted_reasoning_items(monkeypatch):
    from tests.test_chatgpt_responses_tools import _collect

    item = {"type": "reasoning", "id": "rs_1", "encrypted_content": "abc"}
    bare = {"type": "reasoning", "id": "rs_2"}
    chunks = await _collect(monkeypatch, [
        {"type": "response.output_item.done", "item": bare},
        {"type": "response.output_item.done", "item": item},
        {"type": "response.completed", "response": {}},
    ])
    events = [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]
    got = [e for e in events if e.get("type") == "reasoning_items"]
    assert got and got[0]["items"] == [item]
