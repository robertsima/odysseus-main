"""Regression: an open browser tab stopped every scheduled task from running.

`static/app.js` beats /api/activity/heartbeat every 15s for as long as the page
is not hidden, and a beat marked the browser "active" for 45s
(BACKGROUND_TASK_BROWSER_ACTIVE_SECONDS). 15 < 45, so a tab parked on a second
monitor held has_foreground_activity() true permanently, and _check_due_tasks
re-deferred every due task by 15 minutes on each pass. Nothing ever ran.

The beat now says whether a person actually touched the page. Idle keepalives
are liveness only.
"""

import asyncio

import pytest

from src import interactive_gate as gate


@pytest.fixture(autouse=True)
def _reset_gate(monkeypatch):
    monkeypatch.setattr(gate, "_LAST_BROWSER_ACTIVITY", 0.0)
    monkeypatch.setattr(gate, "_LAST_ACTIVITY", 0.0)
    monkeypatch.setattr(gate, "_ACTIVE_REQUESTS", 0)
    monkeypatch.setattr(gate, "_has_active_chat_stream", lambda: False)
    yield


class TestIdleKeepaliveDoesNotBlockBackgroundWork:
    def test_idle_beat_does_not_mark_the_browser_active(self):
        asyncio.run(gate.mark_browser_activity(interactive=False))
        assert gate.has_foreground_activity() is False

    def test_interactive_beat_still_blocks(self):
        asyncio.run(gate.mark_browser_activity(interactive=True))
        assert gate.has_foreground_activity() is True

    def test_default_is_interactive_for_older_clients(self):
        """A cached app.js that predates the flag must behave as it did."""
        asyncio.run(gate.mark_browser_activity())
        assert gate.has_foreground_activity() is True

    def test_activity_expires_so_the_scheduler_gets_a_turn(self, monkeypatch):
        """The whole point: going quiet eventually lets background work run."""
        asyncio.run(gate.mark_browser_activity(interactive=True))
        ttl = gate._browser_active_seconds()
        later = gate.time.monotonic() + ttl + 1
        assert gate.has_foreground_activity(now=later) is False

    def test_idle_beat_does_not_cancel_running_tasks(self):
        stopped = []

        async def _stop(reason=""):
            stopped.append(reason)

        assert asyncio.run(
            gate.maybe_stop_background_tasks_for_heartbeat(_stop, interactive=False)
        ) is False
        assert stopped == []

    def test_interactive_beat_still_cancels(self):
        stopped = []

        async def _stop(reason=""):
            stopped.append(reason)

        assert asyncio.run(
            gate.maybe_stop_background_tasks_for_heartbeat(_stop, interactive=True)
        ) is True
        assert stopped


class TestWorkbenchRunsPollIsPassive:
    def test_agent_strip_poll_does_not_pre_empt_the_work_it_lists(self):
        """static/js/workbench.js polls this every 4s while a chat is open.
        Tracking it killed any scheduled task within one poll interval — the
        `Stopped 2 background scheduler task(s): foreground request
        GET /api/workbench/runs` line in the logs."""
        assert gate.should_track_interactive_request("/api/workbench/runs", "GET") is False

    def test_stopping_a_run_is_still_a_real_interaction(self):
        assert gate.should_track_interactive_request(
            "/api/workbench/runs/abc123/stop", "POST"
        ) is True
