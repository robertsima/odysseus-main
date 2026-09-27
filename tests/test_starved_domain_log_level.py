"""The "no usable tools remain" line is a WARNING for a chat, not for a worker.

Production (2026-09-27): research workers whose loadouts deliberately allow
only web tools logged it ~17 times across four workflows, because their task
text mentioned documents, notes, settings... -- domains that are off for them
on purpose. A top-level chat losing a domain is still worth a warning.
"""
import asyncio
import logging

from src import agent_control
from src.agent_loop import _DOMAIN_TOOL_MAP, repair_starved_domains


def _levels(caplog):
    return [r.levelno for r in caplog.records if "no usable tools remain" in r.getMessage()]


def test_starved_domain_warns_outside_any_event_loop(caplog):
    caplog.set_level(logging.DEBUG, logger="src.agent_loop")
    disabled = set(_DOMAIN_TOOL_MAP["settings"])
    assert repair_starved_domains(set(), {"settings"}, disabled) == ["settings"]
    assert _levels(caplog) == [logging.WARNING]


def test_starved_domain_is_info_inside_a_worker_and_warning_in_a_chat_turn(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger="src.agent_loop")
    disabled = set(_DOMAIN_TOOL_MAP["settings"])
    workers = {}
    monkeypatch.setattr(agent_control, "_WORKERS", workers)

    async def turn():
        return repair_starved_domains(set(), {"settings"}, disabled)

    async def main():
        worker = asyncio.create_task(turn())
        workers["session-run"] = worker  # how launch_worker registers a detached worker
        assert await worker == ["settings"]
        assert await asyncio.create_task(turn()) == ["settings"]  # an ordinary chat turn

    asyncio.run(main())
    assert _levels(caplog) == [logging.INFO, logging.WARNING]


def test_worker_loop_drained_in_its_own_task_is_info(caplog, monkeypatch):
    """Production (2026-09-27 22:30): 12 WARNINGs from research-workflow
    children. They ARE registered in `_WORKERS`, but `run_headless` drains the
    agent loop in a future of its own, so the task that logs is never the
    registered one. The worker's run is marked with `child_run_scope` instead,
    which the drain task inherits."""
    caplog.set_level(logging.DEBUG, logger="src.agent_loop")
    disabled = set(_DOMAIN_TOOL_MAP["settings"])
    workers = {}
    monkeypatch.setattr(agent_control, "_WORKERS", workers)

    async def turn():
        return repair_starved_domains(set(), {"settings"}, disabled)

    async def fake_run_headless():
        # The shape of headless_agent.run_headless: the loop runs in a new task.
        return await asyncio.ensure_future(turn())

    async def worker_run():
        with agent_control.child_run_scope():
            await fake_run_headless()
        # The hand-off continues the PARENT chat after the scope: a top-level turn.
        await asyncio.ensure_future(turn())

    async def main():
        task = asyncio.create_task(worker_run())
        workers["session-run"] = task  # registered exactly as launch_worker does
        await task

    asyncio.run(main())
    assert _levels(caplog) == [logging.INFO, logging.WARNING]


async def test_launch_worker_runs_the_child_inside_child_run_scope(monkeypatch, tmp_path):
    """Every launched worker -- orchestrate_agents and workflow children go
    through launch_worker -- runs its headless loop as a child run."""
    from src import agent_activity, constants
    import src.agent_tools.session_tools as session_tools
    import src.ai_interaction as ai_interaction
    import src.headless_agent as headless

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    agent_activity._reset_for_tests()

    class _Chat:
        def __init__(self, sid):
            self.id, self.name, self.model = sid, sid, "m"
            self.endpoint_url, self.context_length = "http://llm.test/v1", 200_000
            self.messages = []

        def add_message(self, message):
            self.messages.append(message)

        def get_context_messages(self):
            return []

    class _M:
        def get_session(self, sid):
            return {"w-1": chat}.get(sid)

        def save_sessions(self):
            pass

    chat = _Chat("w-1")
    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: _M())
    monkeypatch.setattr(session_tools, "_new_child_session", lambda *a, **k: (chat, None))
    seen = []

    async def fake_headless(sess, messages, **kwargs):
        seen.append(await asyncio.ensure_future(asyncio.sleep(0, agent_control.in_child_run())))
        return "done", []

    monkeypatch.setattr(headless, "run_headless", fake_headless)
    try:
        rec = await agent_control.launch_worker(owner="alice", task="scan the market")
        await agent_control._WORKERS[rec["run_id"]]
    finally:
        agent_activity._reset_for_tests()
    assert seen == [True]
    assert agent_control.in_child_run() is False
