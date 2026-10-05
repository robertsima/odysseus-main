"""A scheduled task only gets the email tools its own text asks for.

Production, 2026-09-28: the nightly "Todoist Inbox Prioritization" task was
offered archive_email, bulk_email and delete_email. The scheduler unions
ASSISTANT_ALWAYS_AVAILABLE — the chat assistant's set, which carries the whole
email suite — into every task's tools, and "Inbox" made the agent's intent
router add the email domain too. Nobody watches a scheduled run, so a
mailbox-changing tool must not be reachable from a task that is not about mail.

Also pinned here: a run preempted by foreground activity hands the tool calls
it already made to its retry, instead of the retry starting blind.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from src.task_scheduler import (
    TASK_EMAIL_READ_TOOLS,
    TASK_EMAIL_WRITE_TOOLS,
    TaskScheduler,
    compose_task_relevant_tools,
    task_domain_disabled_tools,
    task_is_email_scoped,
)
from src.tool_index import ASSISTANT_ALWAYS_AVAILABLE

pytestmark = pytest.mark.security

TODOIST_PROMPT = (
    "Review only the current Todoist Inbox using the official Todoist integration. "
    "Reprioritize clear mismatches using the user's Eisenhower convention: P1 = urgent "
    "and important with a real near-term consequence."
)
TODOIST_TEXT = "Nightly Todoist Inbox Prioritization\n" + TODOIST_PROMPT


@pytest.mark.parametrize("text", [
    TODOIST_TEXT,
    "Summarise today's calendar and notes",
    "Check the GitHub inbox for review requests",
    "Fetch the Miniflux unread feed and post a digest",
])
def test_non_email_tasks_are_not_email_scoped(text):
    assert task_is_email_scoped(text) is False


@pytest.mark.parametrize("text", [
    "Archive newsletters older than a week",
    "Triage my inbox and flag anything urgent",
    "Summarise unread emails from today",
    "Unsubscribe me from marketing mail",
    "Check Gmail for invoices",
])
def test_email_tasks_are_email_scoped(text):
    assert task_is_email_scoped(text) is True


def test_the_todoist_task_gets_no_mailbox_changing_tool():
    disabled = task_domain_disabled_tools(TODOIST_TEXT)
    assert TASK_EMAIL_WRITE_TOOLS <= disabled
    # What production offered: RAG picked email tools on "Inbox", and the
    # always-available set added the rest.
    rag = {"mcp__todoist__todoist", "archive_email", "list_emails", "bulk_email"}
    tools = compose_task_relevant_tools(rag, ASSISTANT_ALWAYS_AVAILABLE, disabled,
                                        task_text=TODOIST_TEXT)
    assert not tools & TASK_EMAIL_WRITE_TOOLS
    assert "mcp__todoist__todoist" in tools
    # Read-only email tools come only from retrieval, never from the
    # always-available set.
    assert "list_emails" in tools
    assert "read_email" not in tools
    # The rest of the assistant set is untouched.
    assert {"manage_tasks", "manage_notes", "web_search", "bash"} <= tools


def test_an_email_task_keeps_the_email_suite():
    text = "Morning email triage\nArchive newsletters and draft replies to anything urgent"
    assert task_domain_disabled_tools(text) == set()
    tools = compose_task_relevant_tools(set(), ASSISTANT_ALWAYS_AVAILABLE, set(), task_text=text)
    assert TASK_EMAIL_WRITE_TOOLS & ASSISTANT_ALWAYS_AVAILABLE <= tools
    assert TASK_EMAIL_READ_TOOLS & ASSISTANT_ALWAYS_AVAILABLE <= tools


def test_a_task_that_names_a_tool_keeps_that_tool():
    # No mail vocabulary at all — only the tool's own name.
    text = "Todoist cleanup\nReview the Todoist Inbox, then call send_email with the result"
    disabled = task_domain_disabled_tools(text)
    assert "send_email" not in disabled
    assert "delete_email" in disabled
    tools = compose_task_relevant_tools(set(), ASSISTANT_ALWAYS_AVAILABLE, disabled, task_text=text)
    assert "send_email" in tools
    assert "delete_email" not in tools


def test_callers_without_task_text_are_unchanged():
    tools = compose_task_relevant_tools(set(), ASSISTANT_ALWAYS_AVAILABLE, None)
    assert set(ASSISTANT_ALWAYS_AVAILABLE) <= tools


def _task(prompt, name="Nightly Todoist Inbox Prioritization", task_id=None):
    return SimpleNamespace(
        id=task_id, crew_member_id=None, endpoint_url="http://ep/v1", model="m",
        session_id="s", owner="admin", prompt=prompt, name=name, max_steps=5,
        character_id=None,
    )


def _patch_deps(monkeypatch, captured, rag=()):
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: [] if key == "disabled_tools" else default,
    )

    class _Index:
        def get_tools_for_query(self, query, k=8):
            return list(rag)

    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: _Index())
    monkeypatch.setattr("src.task_endpoint.resolve_task_candidates", lambda **kw: [])

    async def _stub_stream(**kwargs):
        captured.update(kwargs)
        yield "data: " + json.dumps({"delta": "done"}) + "\n\n"

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", _stub_stream)


async def test_the_scheduled_run_hands_the_agent_loop_no_email_write_tools(monkeypatch):
    captured = {}
    _patch_deps(monkeypatch, captured, rag=["archive_email", "bulk_email", "mcp__todoist__todoist"])
    await TaskScheduler(session_manager=None)._execute_llm_task(_task(TODOIST_PROMPT), db=None)

    assert TASK_EMAIL_WRITE_TOOLS <= set(captured["disabled_tools"])
    assert not set(captured["relevant_tools"]) & TASK_EMAIL_WRITE_TOOLS
    assert "mcp__todoist__todoist" in captured["relevant_tools"]


async def test_an_email_scheduled_run_is_not_narrowed(monkeypatch):
    captured = {}
    _patch_deps(monkeypatch, captured, rag=["archive_email"])
    await TaskScheduler(session_manager=None)._execute_llm_task(
        _task("Archive newsletters in my inbox", name="Inbox zero"), db=None)

    assert not set(captured.get("disabled_tools") or ()) & TASK_EMAIL_WRITE_TOOLS
    assert "archive_email" in captured["relevant_tools"]
    assert "delete_email" in captured["relevant_tools"]


# ── a preempted run tells its retry what it already did ─────────────────── #


async def test_a_preempted_run_passes_its_tool_calls_to_the_retry(monkeypatch):
    monkeypatch.setattr("src.task_endpoint.resolve_task_candidates", lambda **kw: [])
    reached = asyncio.Event()
    prompts = []

    async def _interrupted_stream(**kwargs):
        prompts.append(kwargs["messages"][-1]["content"])
        for i in range(2):
            yield "data: " + json.dumps({
                "type": "tool_output", "tool": "mcp__todoist__todoist",
                "command": f'{{"action": "update", "task": {i}}}', "output": "ok", "exit_code": 0,
            }) + "\n\n"
        reached.set()
        await asyncio.sleep(3600)  # the foreground sweep cancels it here
        yield "data: [DONE]\n\n"

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", _interrupted_stream)
    scheduler = TaskScheduler(session_manager=None)
    task = _task(TODOIST_PROMPT, task_id="task-1")
    run = asyncio.create_task(scheduler._run_agent_loop("http://ep/v1", "m", task, "s"))
    await reached.wait()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    saved = scheduler._interrupted_progress()["task-1"]
    assert [c["tool"] for c in saved] == ["mcp__todoist__todoist"] * 2

    async def _retry_stream(**kwargs):
        prompts.append(kwargs["messages"][-1]["content"])
        yield "data: " + json.dumps({"delta": "finished"}) + "\n\n"

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", _retry_stream)
    result = await scheduler._run_agent_loop("http://ep/v1", "m", task, "s")

    assert result == "finished"
    assert prompts[0] == TODOIST_PROMPT
    retry_prompt = prompts[1]
    assert retry_prompt.startswith(TODOIST_PROMPT)
    assert "interrupted" in retry_prompt
    assert '{"action": "update", "task": 1}' in retry_prompt
    # Consumed: a third run starts clean.
    assert "task-1" not in scheduler._interrupted_progress()


async def test_a_run_without_tool_calls_leaves_nothing_behind(monkeypatch):
    monkeypatch.setattr("src.task_endpoint.resolve_task_candidates", lambda **kw: [])
    reached = asyncio.Event()

    async def _stream(**kwargs):
        reached.set()
        await asyncio.sleep(3600)
        yield "data: [DONE]\n\n"

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", _stream)
    scheduler = TaskScheduler(session_manager=None)
    run = asyncio.create_task(
        scheduler._run_agent_loop("http://ep/v1", "m", _task(TODOIST_PROMPT, task_id="t2"), "s"))
    await reached.wait()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert scheduler._interrupted_progress() == {}
