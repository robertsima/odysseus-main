"""Worker preflight runs off the event loop.

Production, 2026-10-23: launching a worker froze every chat stream for 2.47 s.
Preflight vets workspaces, which realpaths the data roots on the server's NAS
(``vet_workspace`` -> ``_agent_readable_data_subdirs``), and ``_launch_worker``
called it straight from the coroutine.
"""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.asyncio


class _Stop(Exception):
    pass


async def test_launch_worker_runs_preflight_in_a_worker_thread(monkeypatch):
    from src import agent_control, worker_preflight

    monkeypatch.setattr("src.ai_interaction.get_session_manager", lambda: object())
    monkeypatch.setattr(worker_preflight, "worker_unavailable_tools", lambda owner, profile: set())
    seen = {}

    def _preflight(task, **kwargs):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        seen["task"] = task
        raise _Stop

    monkeypatch.setattr(worker_preflight, "run_preflight", _preflight)
    with pytest.raises(_Stop):
        await agent_control._launch_worker(owner="alice", task="Review the notes")
    assert seen == {"on_loop": False, "task": "Review the notes"}
