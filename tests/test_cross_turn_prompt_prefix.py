"""Two consecutive turns of one chat must share their prompt prefix.

The provider caches a request by its prefix: instructions first, then the tool
schemas, then the conversation. On 2026-09-26/27 the first round of nearly
every turn of a ~120k-token ChatGPT-subscription chat logged `cached=0` while
later rounds of the same turn hit 95-99%, so something at the front of the
request changed between turns. It was per-turn state written into that front:

* the private-shell note and the approved-plan note were prepended to byte 0
  of the system prompt, on turns where they applied only;
* a turn whose words named an admin tool ("task", "settings") listed all
  fifteen admin tools in the system prompt and sent the named ones as schemas,
  and the next turn took both out again;
* the email identity rules were appended to the system prompt only on turns
  whose wording said "email"/"send"/"reply";
* a tool set over the sticky cap restarted even when the turn added nothing.

This drives the real agent loop for two turns against the Codex Responses
endpoint and builds the exact Responses payload each first round would send.
"""
import json

import pytest

import src.agent_loop as al
from src.agent_loop import _STICKY_TOOLS, _sticky_tool_selection
from src.llm_core import _build_chatgpt_responses_payload
from src.prompt_security import UNTRUSTED_CONTEXT_POLICY, untrusted_context_message
from tests.test_same_turn_tool_attachment import _collect, _patch

URL = "https://chatgpt.com/backend-api/codex/responses"
MODEL = "gpt-6-luna"
SESSION = "chat-prefix-1"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    _STICKY_TOOLS.clear()
    _patch(monkeypatch, [])
    # A saved email style, so the email identity rules are in play.
    monkeypatch.setattr("src.settings.load_settings", lambda: {"email_writing_style": "Hiya. Best, Rob"})
    # The shell is not sandboxable here, so the private-shell note is sent.
    monkeypatch.setattr("src.shell_sandbox.unavailable_reason", lambda workspace: "no workspace is set")
    yield
    _STICKY_TOOLS.clear()


def _chat_request(history, text, tag):
    """The shape the chat route hands the loop: the static preface, the
    persisted history ending in this request, then request-local context
    (recalled memories, retrieved documents) appended after it."""
    return (
        [
            {"role": "system", "content": "You are Odysseus.", "_persona": True},
            {"role": "system", "content": UNTRUSTED_CONTEXT_POLICY},
        ]
        + [dict(m) for m in history]
        + [{"role": "user", "content": text}]
        + [
            untrusted_context_message("saved memory: pinned context", f"Pinned memory context:\n- {tag} pinned"),
            untrusted_context_message("saved memory: retrieved context", f"Memory context.\n- {tag} recalled"),
            untrusted_context_message("retrieved documents", f"{tag} vault snippet"),
        ]
    )


def _first_round_payload(monkeypatch, messages, **kwargs):
    captured = []

    async def _fake_stream(_candidates, _messages, **kw):
        request = await kw["candidate_request_factory"](0, URL, MODEL, {})
        captured.append(request)
        yield f'data: {json.dumps({"delta": "Done."})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    _collect(al.stream_agent_loop(
        URL, MODEL, messages, max_rounds=1, session_id=SESSION, allow_private=False, **kwargs,
    ))
    request = captured[0]
    return _build_chatgpt_responses_payload(
        MODEL, request["messages"], 0.3, 4096,
        tools=request["kwargs"]["tools"], cache_key=SESSION, target_url_hint=URL,
    )


def _texts(items):
    return [json.dumps(item.get("content")) for item in items]


def test_consecutive_turns_share_the_prefix_through_prior_history(monkeypatch):
    prior = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi, what can I do?"},
    ]
    first_text = "add a task to remind me tomorrow, then send the test results by email"
    first = _first_round_payload(
        monkeypatch,
        _chat_request(prior, first_text, "turn-one"),
        relevant_tools={"read_file", "grep", "bash", "send_email"},
        approved_plan="- [ ] run the tests\n- [ ] email the results",
    )
    second = _first_round_payload(
        monkeypatch,
        _chat_request(
            prior + [{"role": "user", "content": first_text}, {"role": "assistant", "content": "Done."}],
            "thanks, what did the logs say?",
            "turn-two",
        ),
        relevant_tools={"read_file", "grep"},
        approved_plan="- [x] run the tests\n- [ ] email the results",
    )

    # Everything ahead of the conversation is byte-identical.
    assert first["prompt_cache_key"] == second["prompt_cache_key"] == SESSION
    assert first["instructions"] == second["instructions"]
    assert first["tools"] == second["tools"]
    # ...and the conversation shares its prefix through the prior history:
    # nothing request-local sits in front of it.
    assert first["input"][: len(prior)] == second["input"][: len(prior)]
    assert [item.get("role") for item in first["input"][: len(prior)]] == ["user", "assistant"]
    assert "hello" in _texts(first["input"])[0]

    # The per-turn notes still reach the model, beside the request.
    for payload, marker in ((first, "- [ ] run the tests"), (second, "- [x] run the tests")):
        assert "ACTIVE PLAN" not in payload["instructions"]
        assert "Allow private vault reads" not in payload["instructions"]
        tail = " ".join(_texts(payload["input"][len(prior):]))
        assert "ACTIVE PLAN" in tail and marker in tail
        assert "Allow private vault reads" in tail
    # The trusted email rules stay in the system prompt, on both turns.
    assert "Hard identity rule" in first["instructions"]
    # The admin tool the first turn named stays offered, and the system prompt
    # lists only tools the schema list carries.
    offered = {tool.get("name") for tool in second["tools"]}
    assert "manage_tasks" in offered
    assert "`manage_webhooks`" not in second["instructions"]


def test_a_turn_inside_an_oversized_tool_set_does_not_restart_it(monkeypatch):
    monkeypatch.setattr(al, "_STICKY_TOOLS_MAX", 3)
    # A single turn that needed more than the cap sets the remembered set.
    assert _sticky_tool_selection("s", {"a", "b", "c", "d"}) == {"a", "b", "c", "d"}
    # The next turn needs nothing new: keep the set, and the cached prefix.
    assert _sticky_tool_selection("s", {"a", "b"}) == {"a", "b", "c", "d"}
    # One that would grow it past the cap still restarts.
    assert _sticky_tool_selection("s", {"e"}) == {"e"}


def _turn_payloads(monkeypatch, messages, rounds):
    """Every round's Responses payload for one turn; `rounds` is a list of
    (text, native_calls) the fake model answers with."""
    payloads = []

    async def _fake_stream(_candidates, _messages, **kw):
        request = await kw["candidate_request_factory"](0, URL, MODEL, {})
        payloads.append(_build_chatgpt_responses_payload(
            MODEL, request["messages"], 0.3, 4096,
            tools=request["kwargs"]["tools"], cache_key=SESSION, target_url_hint=URL,
        ))
        text, calls = rounds[min(len(payloads) - 1, len(rounds) - 1)]
        if text:
            yield f'data: {json.dumps({"delta": text})}\n\n'
        if calls:
            yield f'data: {json.dumps({"type": "tool_calls", "calls": calls})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    _collect(al.stream_agent_loop(
        URL, MODEL, messages, max_rounds=len(rounds), session_id=SESSION, allow_private=False,
        relevant_tools={"read_file", "grep"},
    ))
    return payloads


def test_the_next_turn_rewrites_only_the_previous_turns_tail(monkeypatch, caplog):
    """Across a tool-using turn, the next turn's first request may drop the
    turn's tool items (they are not persisted) and its request-local context,
    but everything before the previous request stays byte-identical, so the
    cached prefix covers the whole prior history. The fingerprint's
    `first_diff_item` is the check a production bundle can run on this."""
    import logging

    from src import llm_core

    prior = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi, what can I do?"},
    ]
    call = [{"name": "read_file", "arguments": json.dumps({"path": "a.txt"})}]
    first = _turn_payloads(
        monkeypatch, _chat_request(prior, "read a.txt please", "one"),
        [(None, call), (None, call), ("Done.", None)],
    )
    second = _turn_payloads(
        monkeypatch,
        _chat_request(
            prior + [{"role": "user", "content": "read a.txt please"}, {"role": "assistant", "content": "Done."}],
            "thanks", "two",
        ),
        [("ok", None)],
    )
    # Within the turn every round only appends.
    for earlier, later in zip(first, first[1:]):
        assert later["input"][: len(earlier["input"])] == earlier["input"]
    llm_core._PREFIX_FINGERPRINTS.pop(SESSION, None)
    with caplog.at_level(logging.INFO, logger="src.llm_core"):
        llm_core._log_prompt_prefix(SESSION, MODEL, first[-1])
        llm_core._log_prompt_prefix(SESSION, MODEL, second[0])
    line = [r.getMessage() for r in caplog.records if "[prompt-prefix]" in r.getMessage()][-1]
    fields = dict(part.split("=", 1) for part in line.split() if "=" in part)
    assert fields["changed"] in {"none", "history_shrank"}
    # The divergence is at the previous turn's request-local context, after the
    # whole prior history -- never at the front of the conversation.
    assert int(fields["first_diff_item"]) >= len(prior)
    assert second[0]["input"][: len(prior)] == first[-1]["input"][: len(prior)]


def test_a_worker_follow_up_sends_the_chat_turns_instructions(monkeypatch):
    """A worker's hand-back continues the chat through `run_headless` with the
    bare history (`parent.get_context_messages()`): no persona, no prompt-safety
    policy. Its `instructions` then differed from the chat's own turns at byte
    0 (`instr_diff_at=0` at every switch between the two on 2026-09-28), so the
    follow-up and the user's next turn both re-billed the whole chat."""
    prior = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi, what can I do?"},
    ]
    al._CHAT_PREFACES.clear()
    chat_turn = _first_round_payload(
        monkeypatch, _chat_request(prior, "launch the researcher", "one"),
        relevant_tools={"read_file", "grep"},
    )
    history = prior + [
        {"role": "user", "content": "launch the researcher"},
        {"role": "assistant", "content": "Launched."},
        {"role": "user", "content": "[Worker result] researcher finished: 3 findings",
         "metadata": {"source": "worker"}},
    ]
    follow_up = _first_round_payload(monkeypatch, [dict(m) for m in history], relevant_tools={"read_file", "grep"})
    assert follow_up["instructions"] == chat_turn["instructions"]
    assert follow_up["instructions"].startswith("You are Odysseus.")
    assert follow_up["input"][: len(prior)] == chat_turn["input"][: len(prior)]


def test_a_loadout_chats_follow_up_matches_without_a_remembered_preface(monkeypatch):
    """A loadout chat has no persona prompt (the loadout's voice replaces it),
    so the route sends the policy alone -- which is also what a follow-up gets
    after a restart, when nothing is remembered for the chat."""
    prior = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hi."},
    ]
    route_turn = [
        {"role": "system", "content": UNTRUSTED_CONTEXT_POLICY},
        *[dict(m) for m in prior],
        {"role": "user", "content": "check the presets"},
    ]
    chat_turn = _first_round_payload(monkeypatch, route_turn, relevant_tools={"read_file"})
    al._CHAT_PREFACES.clear()
    follow_up = _first_round_payload(
        monkeypatch,
        [*[dict(m) for m in prior], {"role": "user", "content": "check the presets"}],
        relevant_tools={"read_file"},
    )
    assert follow_up["instructions"] == chat_turn["instructions"]
    assert follow_up["instructions"].startswith("Prompt-safety policy")
