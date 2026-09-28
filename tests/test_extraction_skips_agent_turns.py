"""Memory/skill auto-extraction learns only from a person's turns.

Production, 2026-09-28: "Auto-extracted skill: Agent-Assisted Market Research
Synthesis" was logged at 02:56:31, four seconds after a worker follow-up turn
(the chat posting a second summary of a worker's result) finished — which read
as the follow-up having been learned from. It was not: the follow-up runs
headless and never reaches run_post_response_tasks; the extraction had been
queued by the user's own 29-round turn at 02:56:19. But the extraction queue's
"is this session busy" check only knew about streamed turns, so the extractor
ran *alongside* the follow-up for the same chat, the overlap issue #2927 was
meant to rule out.

Pinned here:
* the extraction queue also waits for a headless turn in the same chat;
* a turn whose message is a runtime envelope (a worker's report, an
  agent-to-agent message) is not learned from;
* nothing is learned from a worker chat (a session with a parent_session);
* a turn with no message at all is not learned from.
"""
from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from routes import chat_helpers


def _run_post(monkeypatch, message, settings=None):
    calls = {"memory": 0, "skill": 0}

    mem = types.ModuleType("services.memory.memory_extractor")

    async def fake_extract_and_store(*a, **k):
        calls["memory"] += 1

    mem.extract_and_store = fake_extract_and_store
    monkeypatch.setitem(sys.modules, "services.memory.memory_extractor", mem)

    skill = types.ModuleType("services.memory.skill_extractor")

    async def fake_maybe_extract_skill(*a, **k):
        calls["skill"] += 1

    skill.maybe_extract_skill = fake_maybe_extract_skill
    monkeypatch.setitem(sys.modules, "services.memory.skill_extractor", skill)

    import src.task_endpoint as task_endpoint
    monkeypatch.setattr(task_endpoint, "resolve_task_candidates",
                        lambda url, model, headers, owner=None: [(url, model, headers)])

    import core.database as database
    monkeypatch.setattr(database, "get_session_settings", lambda sid: dict(settings or {}))

    jobs_seen = []

    def fake_spawn(coro):
        jobs_seen.append(coro)

    async def fake_runner(session_id, jobs, max_wait_s=120.0):
        for _, job in jobs:
            await job

    monkeypatch.setattr(chat_helpers, "_run_extraction_jobs_sequentially", fake_runner)
    monkeypatch.setattr(chat_helpers, "_spawn_bg", fake_spawn)
    monkeypatch.setattr(chat_helpers, "needs_auto_name", lambda name: False)

    sess = SimpleNamespace(
        endpoint_url="http://m/v1", model="m", headers={}, history=[object()] * 8,
        name="Research chat", get_context_messages=lambda: [
            {"role": "user", "content": "research agent memory markets"},
            {"role": "assistant", "content": "done"},
        ],
    )
    chat_helpers.run_post_response_tasks(
        sess, SimpleNamespace(save_sessions=lambda: None), "chat-1", message, "reply", None,
        {"auto_memory": True, "auto_skills": True}, MagicMock(), MagicMock(), None,
        agent_rounds=29, agent_tool_calls=28, skills_manager=MagicMock(), owner="alice",
        extract_skills=True,
    )
    for coro in jobs_seen:
        asyncio.run(coro)
    return calls


def test_a_persons_turn_is_learned_from(monkeypatch):
    assert _run_post(monkeypatch, "Research agent memory business opportunities") == {"memory": 1, "skill": 1}


@pytest.mark.parametrize("message", [
    "[Worker ↳ Creative Agent Memory Researcher: Conduct a scoped market research finished]\nTask: …",
    "[Message from agent session Researcher] here are my notes",
    "",
    "   ",
    None,
])
def test_runtime_envelopes_and_empty_turns_are_not_learned_from(monkeypatch, message):
    assert _run_post(monkeypatch, message) == {"memory": 0, "skill": 0}


def test_a_worker_chat_is_not_learned_from(monkeypatch):
    calls = _run_post(monkeypatch, "Summarise the three sources",
                      settings={"parent_session": "parent-chat"})
    assert calls == {"memory": 0, "skill": 0}


def test_turn_is_learnable_reads_text_blocks():
    assert chat_helpers._turn_is_learnable(
        "no-such-session", [{"type": "text", "text": "plan my week"}]) is True
    assert chat_helpers._turn_is_learnable(
        "no-such-session", [{"type": "image_url", "image_url": {"url": "x"}}]) is False


def test_the_extraction_queue_waits_for_a_headless_turn_in_the_same_chat():
    from src import agent_runs

    assert chat_helpers._is_session_stream_active("chat-headless") is False
    with agent_runs.track_external("chat-headless", source="worker", owner="alice"):
        assert chat_helpers._is_session_stream_active("chat-headless") is True
        assert chat_helpers._is_session_stream_active("another-chat") is False
    assert chat_helpers._is_session_stream_active("chat-headless") is False


async def test_queued_extraction_does_not_overlap_the_worker_follow_up():
    from src import agent_runs

    events = []

    async def job():
        events.append("extract")

    follow_up_done = asyncio.Event()

    async def follow_up():
        with agent_runs.track_external("chat-2", source="worker", owner="alice"):
            events.append("follow-up start")
            await asyncio.sleep(0.3)
            events.append("follow-up end")
        follow_up_done.set()

    fu = asyncio.create_task(follow_up())
    await asyncio.sleep(0.01)
    await chat_helpers._run_extraction_jobs_sequentially("chat-2", [("skill", job())], max_wait_s=5)
    await fu
    assert events == ["follow-up start", "follow-up end", "extract"]
