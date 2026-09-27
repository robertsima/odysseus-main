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
