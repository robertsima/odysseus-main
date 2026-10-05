"""The scheduler's Lotus reminder loop visits every account and survives one failing.

Lotus check-ins and reminders fire only from this loop. If it skips an
owner, or one owner's error ends it, nobody gets a nudge until a restart.
"""
import asyncio

import src.builtin_actions
import src.lotus_notifications
from src.task_scheduler import TaskScheduler


def _run_one_pass(monkeypatch, owners, action):
    scheduler = TaskScheduler(session_manager=None)
    scheduler._running = True

    async def fake_sleep(seconds):
        # The 45 s start offset is skipped; the 60 s wait ends the pass.
        if seconds >= 60:
            scheduler._running = False

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(src.lotus_notifications, "known_owners", lambda: owners)
    monkeypatch.setattr(src.builtin_actions, "action_lotus_reminders", action)
    asyncio.run(scheduler._lotus_pings_loop())


def test_every_owner_is_scanned_even_when_one_scan_fails(monkeypatch):
    scanned = []

    async def action(owner, **kwargs):
        scanned.append(owner)
        if owner == "alice":
            raise RuntimeError("alice's store is unreadable")
        return "ok", True

    _run_one_pass(monkeypatch, ["alice", "bob"], action)

    assert scanned == ["alice", "bob"]


def test_stop_cancels_the_running_lotus_loop():
    async def scenario():
        scheduler = TaskScheduler(session_manager=None)
        scheduler._running = True
        scheduler._lotus_pings_task = asyncio.create_task(asyncio.sleep(3600))
        await asyncio.sleep(0)
        await scheduler.stop()
        return scheduler._lotus_pings_task.cancelled()

    assert asyncio.run(scenario())
