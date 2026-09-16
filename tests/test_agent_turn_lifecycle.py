"""An agent turn is a server-side object, not a thing the browser drives.

The server already got this right — `agent_runs.start()` detaches the run and
the SSE response is only a subscriber, with `/api/chat/resume`, `/api/chat/stop`
and `/api/chat/stream_status` to re-attach. The client did not trust it, which
produced two user-visible faults:

1. **Losing the SSE looked like losing the run.** On a connection-class error the
   client injected a synthetic user turn ("The stream dropped before you
   finished… reply with just: DONE") into a conversation whose agent was still
   working. That is where the stray one-word `DONE` rounds came from. It now asks
   `/api/chat/stream_status` first and re-attaches when the run is alive.

2. **A message typed mid-loop waited for the whole loop.** It was queued as the
   next turn, so on a 56-round turn the user's correction landed ~40 minutes
   late. The agent loop already drains a steer queue between rounds; the
   composer now uses it.

Covered here is the server half of (2), which is where the data-loss risk is: a
steer must never be accepted when nothing will drain it, and must never outlive
the turn it was meant for.
"""

import pytest


@pytest.fixture(autouse=True)
def _clear_steer_queue():
    from src import agent_control

    agent_control._STEER.clear()
    yield
    agent_control._STEER.clear()


class TestSteerQueueLifecycle:
    def test_queued_then_drained_in_order(self):
        from src import agent_control

        agent_control.steer("s", "first")
        agent_control.steer("s", "second")
        assert agent_control.drain_steer("s") == ["first", "second"]
        # Draining empties it, so the next round does not re-apply the same
        # correction.
        assert agent_control.drain_steer("s") == []

    def test_clear_returns_and_empties(self):
        """A turn ending must not leave a message behind to ambush a later turn."""
        from src import agent_control

        agent_control.steer("s", "late arrival")
        assert agent_control.clear_steer("s") == ["late arrival"]
        assert agent_control.pending_steer("s") == []
        assert agent_control.clear_steer("s") == []

    def test_clear_tolerates_no_session(self):
        from src import agent_control

        assert agent_control.clear_steer(None) == []
        assert agent_control.clear_steer("") == []

    def test_queue_is_per_session(self):
        from src import agent_control

        agent_control.steer("a", "for a")
        agent_control.steer("b", "for b")
        assert agent_control.drain_steer("a") == ["for a"]
        assert agent_control.pending_steer("b") == ["for b"] or \
            [r["text"] for r in agent_control.pending_steer("b")] == ["for b"]

    def test_empty_text_is_rejected(self):
        from src import agent_control

        with pytest.raises(ValueError):
            agent_control.steer("s", "   ")

    def test_queue_is_bounded(self):
        """Otherwise a stuck agent plus an impatient user is an unbounded buffer."""
        from src import agent_control

        for i in range(agent_control._STEER_MAX):
            agent_control.steer("s", f"m{i}")
        with pytest.raises(ValueError):
            agent_control.steer("s", "one too many")


class TestSteerability:
    """Only an agent turn drains the queue, and only between rounds. Accepting a
    steer for anything else swallows the user's message outright."""

    def test_not_steerable_for_a_plain_chat_turn(self, monkeypatch):
        """A single-shot reply is "busy" but never reaches the loop, so a steer
        would sit unread and then ambush an unrelated later turn."""
        from routes import chat_routes
        from src import agent_control

        monkeypatch.setattr(chat_routes, "_active_streams", {"s": {"mode": "chat"}}, raising=False)
        assert agent_control.is_steerable("s") is False

    def test_steerable_for_a_running_agent_turn(self, monkeypatch):
        from routes import chat_routes
        from src import agent_control

        monkeypatch.setattr(chat_routes, "_active_streams", {"s": {"mode": "agent"}}, raising=False)
        assert agent_control.is_steerable("s") is True

    def test_steerable_for_a_detached_worker_with_no_chat_stream(self, monkeypatch):
        """Workers, background jobs and pipelines run through the agent loop but
        have no _active_streams entry (agent_runs.track_external). Requiring one
        silently broke steering for every one of them."""
        from routes import chat_routes
        from src import agent_control

        monkeypatch.setattr(chat_routes, "_active_streams", {}, raising=False)
        assert agent_control.is_steerable("worker-1") is True

    def test_route_is_what_requires_the_run_to_be_live(self, monkeypatch):
        """is_steerable only judges "would anything drain it"; the route keeps
        the separate is_busy check for "is anything running at all"."""
        from routes import chat_routes
        from src import agent_control

        monkeypatch.setattr(chat_routes, "_active_streams", {}, raising=False)
        assert agent_control.is_steerable("never-existed") is True


class TestClientTrustsTheServer:
    """Guard the two client invariants with source assertions — the browser code
    has no test harness here, and both regressions are one edit away."""

    @staticmethod
    def _chat_js() -> str:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        return (root / "static" / "js" / "chat.js").read_text(encoding="utf-8", errors="replace")

    def test_auto_recover_probes_the_server_before_nudging(self):
        """A dropped SSE is not a dropped run; check before inventing a turn."""
        src = self._chat_js()
        start = src.index("function _tryAutoRecover")
        body = src[start:start + 4000]
        assert "stream_status" in body, "auto-recover must ask the server if the run is alive"
        assert "resumeStream" in body, "a live run must be re-attached, not nudged"
        # The handshake text must still be reachable for the genuinely-dead case.
        assert "The stream dropped before you finished" in body

    def test_mid_run_send_tries_steer_first(self):
        src = self._chat_js()
        start = src.index("export function queueStreamingComposerRequest")
        body = src[start:start + 2500]
        assert "/steer" in body, "a mid-run message should reach the running agent"
        assert "fallbackToQueue" in body, "and still queue when steering is not possible"

    def test_dashboard_polls_are_marked_as_polls(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        src = (root / "static" / "js" / "agentsDashboard.js").read_text(encoding="utf-8", errors="replace")
        assert "X-Odysseus-Poll" in src
        # The timer-driven reads must use it, or the dashboard resumes killing
        # the background runs it exists to display.
        assert "apiPoll('/api/agents/overview')" in src
        assert "apiPoll('/api/agents/approvals')" in src
