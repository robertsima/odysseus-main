"""A short reply that approves the assistant's proposal is about that proposal.

Production, 2026-09-27 (admin chat, gpt-6-luna). The day before, the user had
asked for a RAG retrieval audit that stalled. Then:

* 13:50 user: "Can we make a sign in flow that would work from odysseus
  initiating it and i can complete without ssh into zimaos"; the assistant
  answered with a concrete "Proposed flow: 1. Settings → Claude Code → Sign
  in …".
* 13:54 user: "i like that idea, you can use odysseus agents to inplement and
  push". Logged ``continuation=False low_signal=True domains=[]``. The agent
  asked for the status of every worker the chat had ever started, started a
  worktree named ``rag-retrieval-health-improvements`` and delegated "RAG
  retrieval audit…" runs: the previous day's task.
* 13:56 a mid-task correction was injected and the turn carried on regardless;
  the user pressed Stop, which left an empty assistant message in history.

Covered here: the reply is recognised and anchored to the assistant message it
answers (intent, retrieval query, and a harness directive placed directly
before it); a launch whose task shares nothing with what was approved is
refused once; ``manage_agent_loadout status`` honours ``name``; a mid-task
correction carries a re-check directive; a stop before any reply reads to the
model as an explicit marker.
"""

import json

import pytest

import src.agent_loop as al
from core.models import ChatMessage, Session
from routes.chat_helpers import build_retrieval_query
from src.intent_assessment import (
    STOPPED_BEFORE_REPLY_TEXT,
    assess_request,
    clip_proposal,
    is_proposal_reply,
    last_assistant_reply,
    proposal_reply_anchor,
)
from src.objective_guard import distinctive_words, launch_task_text, stale_objective_refusal
from src.prompt_security import UNTRUSTED_CONTEXT_POLICY, untrusted_context_message
from src.user_time import current_datetime_context_message
from tests.src.agent_loop.test_same_turn_tool_attachment import _collect, _events, _patch

OLD_TASK = (
    "audit and improve RAG retrieval/chunking in the vault indexer, implement using "
    "Claude Opus 5.5, push to dev"
)
ASK = (
    "Can we make a sign in flow that would work from odysseus initiating it and i can "
    "complete without ssh into zimaos"
)
PROPOSAL = (
    "Proposed flow: 1. Settings → Claude Code → Sign in. Odysseus starts `claude "
    "setup-token` inside the container and captures the verification URL. 2. You open "
    "the URL, approve, and paste the code back into the dialog. 3. Odysseus stores the "
    "credential and re-checks status. Want me to build it?"
)
REPLY = "i like that idea, you can use odysseus agents to inplement and push"


def _incident_history(reply=REPLY):
    return [
        {"role": "user", "content": OLD_TASK},
        {"role": "assistant", "content": "The RAG audit stalled: Claude Code is too old for Opus 5.5."},
        {"role": "user", "content": ASK},
        {"role": "assistant", "content": PROPOSAL},
        {"role": "user", "content": reply},
        # The turn's context envelopes, as the chat route appends them.
        untrusted_context_message("saved memory", "Name: Rob. GitHub: robsima"),
        untrusted_context_message("skills index", "rag-tuning, vault-maintenance"),
        current_datetime_context_message(),
    ]


# ── recognising the reply ───────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    REPLY, "I like that idea", "sounds good", "Sounds good!", "let's do that", "go ahead",
    "do it", "yes please", "Yes, please.", "can u do that", "can you do that?", "that",
    "implement it", "ship it", "ok go ahead", "that works", "LGTM", "do it anyway",
    "great, let's do it and push to dev", "sounds good but use sonnet instead",
])
def test_short_approvals_are_proposal_replies(text):
    assert is_proposal_reply(text)


@pytest.mark.parametrize("text", [
    "ok", "okay", "ok thanks", "thanks", "hello", "do this again", "do it again",
    "that failed", "that is wrong", "the first one",
    "sounds good, now what is the weather in Paris?",
    "yes, search the web for news",
    "go ahead and email Chris the report",
    "I like that idea but first explain how caching works",
    ASK,
    "sounds good, " + "word " * 20,
])
def test_new_requests_and_acknowledgements_are_not(text):
    assert not is_proposal_reply(text)


def test_the_reply_is_anchored_past_the_context_envelopes():
    history = _incident_history()
    assert last_assistant_reply(history) == PROPOSAL
    assert proposal_reply_anchor(history) == PROPOSAL


def test_the_anchor_is_only_the_reply_directly_before_this_turn():
    # A tool-call-only assistant round and the tool output are skipped; the
    # previous human turn ends the search.
    history = [
        {"role": "user", "content": ASK},
        {"role": "assistant", "content": PROPOSAL},
        {"role": "user", "content": "what time is it"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "12:00", "tool_call_id": "1"},
        {"role": "assistant", "content": "It is noon."},
        {"role": "user", "content": "go ahead"},
    ]
    assert proposal_reply_anchor(history) == "It is noon."
    # Stopped before replying: there is no reply to anchor to.
    stopped = history[:-2] + [
        {"role": "assistant", "content": STOPPED_BEFORE_REPLY_TEXT},
        {"role": "user", "content": "go ahead"},
    ]
    assert proposal_reply_anchor(stopped) == ""
    # First turn: nothing to anchor to.
    assert proposal_reply_anchor([{"role": "user", "content": "go ahead"}]) == ""


def test_assessment_is_a_continuation_that_retrieves_the_proposal():
    assessment = assess_request(_incident_history())
    assert assessment.continuation and not assessment.low_signal
    assert "Settings → Claude Code → Sign in" in assessment.retrieval_query
    assert ASK in assessment.retrieval_query
    # The older task is not what retrieval follows.
    assert "RAG" not in assessment.retrieval_query
    assert assessment.proposal_excerpt == clip_proposal(PROPOSAL)


def test_agent_classifier_matches_the_incident_turn():
    intent = al._classify_agent_request(_incident_history(), REPLY)
    assert intent["continuation"] is True
    assert intent["low_signal"] is False
    assert intent["proposal_anchor"] == PROPOSAL
    assert "Sign in" in intent["retrieval_query"] and "RAG" not in intent["retrieval_query"]


def test_a_new_request_keeps_its_own_retrieval_query():
    history = _incident_history("search my notes for the zimaos backup schedule")
    intent = al._classify_agent_request(history, "search my notes for the zimaos backup schedule")
    assert intent["proposal_anchor"] == ""
    assert intent["retrieval_query"] == "search my notes for the zimaos backup schedule"


def test_long_proposals_are_clipped_head_and_tail():
    long = "Plan: " + "step " * 400 + "Want me to build it?"
    clipped = clip_proposal(long)
    assert len(clipped) <= 610
    assert clipped.startswith("Plan: ") and clipped.endswith("Want me to build it?")


def test_memory_retrieval_query_follows_the_proposal():
    history = _incident_history()[:5]
    query, mode = build_retrieval_query(REPLY, history)
    assert mode == "proposal_reply"
    assert REPLY in query and ASK in query and "Sign in" in query
    assert "RAG" not in query


# ── the directive reaches the model directly before the reply ───────────────

URL = "https://api.openai.com/v1"


def _capture_first_request(monkeypatch, messages, **kwargs):
    sent = []

    async def _fake_stream(_candidates, route_messages, **kw):
        sent.append([dict(m) for m in route_messages])
        yield f'data: {json.dumps({"delta": "On it."})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    _collect(al.stream_agent_loop(URL, "gpt-4o", messages, max_rounds=1, session_id="anchor-1",
                                  allow_private=False, **kwargs))
    return sent[0]


def _chat_request(history_and_reply):
    return [{"role": "system", "content": "You are Odysseus."},
            {"role": "system", "content": UNTRUSTED_CONTEXT_POLICY}] + history_and_reply


def _text(message):
    content = message.get("content")
    return content if isinstance(content, str) else json.dumps(content)


def test_directive_is_the_message_directly_before_the_reply(monkeypatch):
    _patch(monkeypatch, [])
    sent = _capture_first_request(monkeypatch, _chat_request(_incident_history()))
    reply_idx = max(i for i, m in enumerate(sent) if _text(m) == REPLY)
    directive = _text(sent[reply_idx - 1])
    assert directive.startswith("[Harness directive — from the runtime, not the user]")
    assert "The user is replying to your previous message" in directive
    assert "Settings → Claude Code → Sign in" in directive
    assert "Do not resume older tasks" in directive
    # The envelopes are still there, ahead of the directive; the proposal is
    # still the assistant message in history ahead of them.
    before = [_text(m) for m in sent[:reply_idx - 1]]
    assert any("robsima" in t for t in before)
    assert any(t == PROPOSAL for t in before)
    # The reply is the last thing the model reads.
    assert reply_idx == len(sent) - 1


def test_no_directive_on_an_ordinary_turn(monkeypatch):
    _patch(monkeypatch, [])
    history = _incident_history("search my notes for the zimaos backup schedule")
    sent = _capture_first_request(monkeypatch, _chat_request(history))
    assert not any("The user is replying to your previous message" in _text(m) for m in sent)


def test_the_prior_history_prefix_is_unchanged_by_the_directive(monkeypatch):
    _patch(monkeypatch, [])
    approval = _capture_first_request(monkeypatch, _chat_request(_incident_history()))
    ordinary = _capture_first_request(
        monkeypatch, _chat_request(_incident_history("search my notes for the zimaos backup schedule")))
    # Everything through the proposal is byte-identical: the directive sits at
    # the tail beside the request, never in front of the cached history.
    upto = next(i for i, m in enumerate(approval) if _text(m) == PROPOSAL) + 1
    assert [_text(m) for m in approval[1:upto]] == [_text(m) for m in ordinary[1:upto]]


# ── stale-objective check ───────────────────────────────────────────────────

APPROVED = [REPLY, PROPOSAL]


def test_launch_text_is_read_only_from_launching_actions():
    assert launch_task_text("manage_agent_worktree", '{"action":"start","name":"rag-x"}') == "rag-x"
    assert launch_task_text("manage_agent_worktree", '{"action":"status"}') is None
    assert launch_task_text("manage_agent_worktree", "{}") is None  # default action is status
    assert launch_task_text("delegate_to_claude_code", '{"prompt":"do x"}') == "do x"  # default run
    assert launch_task_text("delegate_to_agent", '{"action":"poll","task_id":"t"}') is None
    assert launch_task_text("manage_agent_loadout", '{"action":"start","name":"Lead","task":"t1"}') == "t1"
    assert launch_task_text("manage_agent_loadout", '{"action":"status","name":"Lead"}') is None
    assert launch_task_text("bash", '{"command":"ls"}') is None
    assert launch_task_text("manage_agent_worktree", "not json") is None


def test_generic_words_are_not_distinctive():
    assert distinctive_words("implement and push with odysseus agents to dev") == set()
    assert "retrie" in distinctive_words("rag-retrieval-health-improvements")


@pytest.mark.parametrize("tool,args", [
    ("manage_agent_worktree", {"action": "start", "name": "rag-retrieval-health-improvements"}),
    ("delegate_to_claude_code", {"action": "start", "prompt": (
        "Audit and improve RAG retrieval and chunking in the vault indexer; measure recall on "
        "the eval set, tune chunk sizes and overlap, add reranking, run tests, commit to dev. "
        "Use Claude Opus 5.5.")}),
    ("manage_agent_loadout", {"action": "start", "name": "Lead Engineer",
                              "task": "RAG reliability improvements with Opus 5.5"}),
])
def test_the_incident_launches_are_refused(tool, args):
    message = stale_objective_refusal(tool, json.dumps(args), APPROVED)
    assert message and "shares nothing with what the user just approved" in message
    assert "ask_user" in message


@pytest.mark.parametrize("tool,args", [
    ("manage_agent_worktree", {"action": "start", "name": "claude-code-sign-in"}),
    ("delegate_to_claude_code", {"action": "start", "prompt": (
        "Implement a Claude Code sign-in flow: a Settings button that starts setup-token in "
        "the container, surfaces the verification URL, accepts the pasted code and re-checks "
        "status. Add tests.")}),
    ("delegate_to_claude_code", {"action": "poll", "task_id": "t-1"}),
])
def test_launches_of_the_approved_work_pass(tool, args):
    assert stale_objective_refusal(tool, json.dumps(args), APPROVED) is None


def test_a_mid_turn_correction_extends_what_was_approved():
    args = json.dumps({"action": "start", "name": "gemini-signin-provider"})
    assert stale_objective_refusal("manage_agent_worktree", args, APPROVED)
    assert stale_objective_refusal(
        "manage_agent_worktree", args, APPROVED + ["You can use gemini instead of claude"]) is None


def _run_rounds(monkeypatch, messages, rounds, **kwargs):
    sent = []

    async def _fake_stream(_candidates, route_messages, **kw):
        idx = len(sent)
        sent.append([dict(m) for m in route_messages])
        text, calls = rounds[min(idx, len(rounds) - 1)]
        if text:
            yield f'data: {json.dumps({"delta": text})}\n\n'
        if calls:
            yield f'data: {json.dumps({"type": "tool_calls", "calls": calls})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    events = _events(_collect(al.stream_agent_loop(
        URL, "gpt-4o", messages, max_rounds=6, session_id=kwargs.pop("session_id", "anchor-2"),
        allow_private=False, **kwargs)))
    return events, sent


STALE_START = [{"name": "manage_agent_worktree",
                "arguments": json.dumps({"action": "start", "name": "rag-retrieval-health-improvements"})}]


def test_loop_refuses_the_stale_launch_once(monkeypatch):
    calls = []
    _patch(monkeypatch, calls)
    events, sent = _run_rounds(
        monkeypatch, _chat_request(_incident_history()),
        [(None, STALE_START), ("Working on the sign-in flow instead.", None)],
        relevant_tools={"manage_agent_worktree"},
    )
    assert calls == []  # never executed
    outputs = [e for e in events if e.get("type") == "tool_output"]
    assert outputs and "shares nothing with what the user just approved" in json.dumps(outputs[0])
    assert "shares nothing with what the user just approved" in json.dumps(sent[1])


def test_loop_lets_the_identical_call_through_the_second_time(monkeypatch):
    calls = []
    _patch(monkeypatch, calls)
    events, _sent = _run_rounds(
        monkeypatch, _chat_request(_incident_history()),
        [(None, STALE_START), ("It is part of it.", STALE_START), ("Done.", None)],
        relevant_tools={"manage_agent_worktree"},
    )
    outputs = [json.dumps(e) for e in events if e.get("type") == "tool_output"]
    assert len(outputs) == 2
    assert "shares nothing with what the user just approved" in outputs[0]
    # Refused once, not every time: the repeat goes on to the normal gates
    # (here the approval gate), so a genuine sub-task is delayed, never blocked.
    assert "shares nothing with what the user just approved" not in outputs[1]
    assert any(e.get("type") == "ask_user" for e in events)


def test_loop_does_not_check_an_ordinary_turn(monkeypatch):
    calls = []
    _patch(monkeypatch, calls)
    history = [
        {"role": "user", "content": "start a worktree for the rag retrieval fixes"},
    ]
    events, _sent = _run_rounds(
        monkeypatch, _chat_request(history),
        [(None, STALE_START), ("Started.", None)],
        relevant_tools={"manage_agent_worktree"},
    )
    assert "shares nothing with what the user just approved" not in json.dumps(events)


# ── a mid-task correction carries a re-check ────────────────────────────────

@pytest.fixture
def _steer_queue(tmp_path, monkeypatch):
    from src import agent_activity, agent_control, constants

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    agent_activity._reset_for_tests()
    agent_control._STEER.clear()
    yield agent_control
    agent_control._STEER.clear()
    agent_activity._reset_for_tests()


def test_user_steer_is_followed_by_a_recheck_directive(monkeypatch, _steer_queue):
    _patch(monkeypatch, [])
    _steer_queue.steer("steer-1", "You can use other models outside of claude")
    _events_, sent = _run_rounds(
        monkeypatch, _chat_request([{"role": "user", "content": "build the sign in flow"}]),
        [("Okay.", None)], session_id="steer-1", relevant_tools={"read_file"},
    )
    texts = [_text(m) for m in sent[0]]
    steer_idx = texts.index("[Mid-task instruction from the user] You can use other models outside of claude")
    directive = texts[steer_idx + 1]
    assert directive.startswith("[Harness directive — from the runtime, not the user]")
    # The steer is the message just above; the directive does not quote it again.
    assert "outside of claude" not in directive
    assert "changes the objective" in directive and "next tool call" in directive
    assert "change course now" in directive


# ── loadout status honours name ─────────────────────────────────────────────

async def test_loadout_status_filters_by_name(monkeypatch, tmp_path):
    from src import agent_activity, constants
    from src.agent_tools.loadout_tools import manage_agent_loadout

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    agent_activity._reset_for_tests()
    for i, profile in enumerate(("Lead Engineer", "Scout", "Scout", "Lead Engineer")):
        rid = f"session-{i}"
        agent_activity.run_started(f"w-{i}", "session", f"Worker {i}", run_id=rid, owner="u",
                                   data={"parent_session": "chat-9", "target_session": f"w-{i}",
                                         "profile": profile})
        agent_activity.run_finished(f"w-{i}", "session", rid, f"Worker {i} failed", status="failed",
                                    owner="u", data={"target_session": f"w-{i}"})
    try:
        everything = await manage_agent_loadout('{"action": "status"}', "chat-9", owner="u")
        assert len(everything["runs"]) == 4

        lead = await manage_agent_loadout(
            '{"action": "status", "name": "lead engineer"}', "chat-9", owner="u")
        assert lead["exit_code"] == 0
        assert {row["loadout"] for row in lead["runs"]} == {"Lead Engineer"}
        assert len(lead["runs"]) == 2
        assert "with loadout 'lead engineer'" in lead["response"]

        none = await manage_agent_loadout('{"action": "status", "name": "Auditor"}', "chat-9", owner="u")
        assert none["exit_code"] == 0 and none["runs"] == []
        assert "no worker runs with loadout 'Auditor'" in none["response"]
    finally:
        agent_activity._reset_for_tests()


# ── a stop before any reply reads as a marker ───────────────────────────────

def test_stopped_empty_assistant_reads_as_a_marker_to_the_model():
    session = Session(id="s-stop", name="t", endpoint_url="", model="m")
    session.history = [
        ChatMessage("user", REPLY),
        ChatMessage("assistant", "", metadata={"stopped": True, "cancelled": True, "model": "m"}),
        ChatMessage("assistant", "partial answer", metadata={"stopped": True}),
        ChatMessage("assistant", ""),
    ]
    context = session.get_context_messages()
    assert context[1]["content"] == STOPPED_BEFORE_REPLY_TEXT
    assert context[1]["metadata"]["cancelled"] is True
    assert context[2]["content"] == "partial answer"
    assert context[3]["content"] == ""  # not a stop: left alone
    # The stored message (and the UI bubble rendered from it) is unchanged.
    assert session.history[1].content == ""


def test_inject_messages_log_describes_records_without_text():
    from routes.session_routes import _injected_message_kind

    assert _injected_message_kind(
        {"role": "assistant", "content": "", "metadata": {"stopped": True, "cancelled": True}}
    ) == "assistant:stopped"
    assert _injected_message_kind({"role": "user", "content": "secret words"}) == "user:text"
    assert _injected_message_kind({"role": "assistant", "content": " "}) == "assistant:empty"
