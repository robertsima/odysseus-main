"""A task that needs tools must not complete on the no-tools prose fallback.

Production, 2026-10-21: the model endpoint rejected the agent loop as
unsupported, the scheduler fell back to a plain call with no tools, the model
answered in prose, and the run was recorded as completed with no evidence the
tool work happened. Plain-text tasks keep the fallback.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.asyncio


def _task(prompt):
    return SimpleNamespace(
        crew_member_id=None, endpoint_url="http://ep/v1", model="m",
        session_id="s", owner="admin", prompt=prompt,
        name="job", max_steps=5, character_id=None,
    )


def _setup(monkeypatch, prose="Done, I updated everything."):
    monkeypatch.setattr(
        "src.settings.get_setting",
        lambda key, default=None: [] if key == "disabled_tools" else default,
    )
    monkeypatch.setattr("src.tool_index.get_tool_index", lambda: None)
    monkeypatch.setattr("src.tool_policy.live_tool_names", lambda: {"mcp__todoist__todoist", "bash"})
    calls = []

    async def _call(messages, **kw):
        calls.append(messages)
        return prose

    import src.task_endpoint as _te
    monkeypatch.setattr(_te, "task_llm_call_async", _call)

    from src.task_scheduler import TaskScheduler
    sched = TaskScheduler(session_manager=None)

    async def _unsupported(*a, **k):
        raise RuntimeError("400: model does not support tools")

    sched._run_agent_loop = _unsupported
    return sched, calls


async def test_tool_task_is_not_completed_by_the_prose_fallback(monkeypatch):
    sched, calls = _setup(monkeypatch)
    task = _task("Reprioritize my inbox with mcp__todoist__todoist")
    with pytest.raises(RuntimeError) as err:
        await sched._execute_llm_task(task, db=None)
    text = str(err.value)
    assert "does not support tools" in text
    assert "needs tools" in text
    assert calls == []  # no prose call whose answer could pass for the result


async def test_tool_task_keeps_the_transient_retry_on_a_network_blip(monkeypatch):
    import httpx

    sched, calls = _setup(monkeypatch)

    async def _blip(*a, **k):
        raise httpx.ConnectError("connection refused")

    sched._run_agent_loop = _blip
    with pytest.raises(httpx.ConnectError):
        await sched._execute_llm_task(_task("Reprioritize my inbox with mcp__todoist__todoist"), db=None)


async def test_plain_text_task_still_completes_on_the_fallback(monkeypatch):
    sched, calls = _setup(monkeypatch)
    result = await sched._execute_llm_task(_task("Write a haiku about autumn"), db=None)
    assert result == "Done, I updated everything."


async def test_crew_with_a_tool_allowlist_needs_tools(monkeypatch):
    from src.task_scheduler import task_needs_tools

    crew = SimpleNamespace(enabled_tools='["web_search"]', agent_profile=None)
    assert task_needs_tools("Write a haiku", crew, set()) is True
    unscoped = SimpleNamespace(enabled_tools="all", agent_profile=None)
    assert task_needs_tools("Write a haiku", unscoped, set()) is False
