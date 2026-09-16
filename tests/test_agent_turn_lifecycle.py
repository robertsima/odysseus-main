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
def _clear_steer_queue(tmp_path, monkeypatch):
    from src import agent_activity, agent_control, constants

    # Queueing a steer writes to the activity feed (that feed is where a
    # steering message's history lives), so the data directory is relocated for
    # every case here — otherwise these tests would append to the real one.
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    agent_activity._reset_for_tests()
    agent_control._STEER.clear()
    yield
    agent_control._STEER.clear()
    agent_activity._reset_for_tests()


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


class TestLiveChildCapacity:
    def test_queued_claude_task_counts_before_activity_run_exists(self, monkeypatch):
        """Claude records a task before its semaphore permits `_run_claude`.
        That queued interval must consume the same per-chat worker slot as a
        published run, or several starts in one agent round all pass the gate.
        """
        from src import agent_activity, agent_control
        from src.agent_tools import claude_code_tools

        class Runner:
            def summaries(self, *, limit):
                assert limit == 400
                return [
                    {"task_id": "queued-task", "session_id": "chat-1", "status": "queued"},
                    {"task_id": "other-chat", "session_id": "chat-2", "status": "queued"},
                ]

        monkeypatch.setattr(agent_activity, "list_runs", lambda *, limit: [])
        monkeypatch.setattr(claude_code_tools, "get_task_runner", lambda: Runner())
        assert agent_control.live_children("chat-1") == 1

    def test_claude_task_already_in_activity_is_not_double_counted(self, monkeypatch):
        from src import agent_activity, agent_control
        from src.agent_tools import claude_code_tools

        class Runner:
            def summaries(self, *, limit):
                return [{"task_id": "task-1", "session_id": "chat-1", "status": "running"}]

        monkeypatch.setattr(agent_activity, "list_runs", lambda *, limit: [
            {"run_id": "task-1", "session_id": "chat-1", "status": "running", "source": "claude_code"},
        ])
        monkeypatch.setattr(claude_code_tools, "get_task_runner", lambda: Runner())
        assert agent_control.live_children("chat-1") == 1


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

    def test_mid_run_steer_chip_tracks_the_server_lifecycle(self):
        """The optimistic composer chip used to have no steer id, so it could
        not observe injection and stayed labelled `Steering` forever."""
        src = self._chat_js()
        submit = src[src.index("export function queueStreamingComposerRequest"):]
        lifecycle = src[src.index("function _applySteeredBubbleState"):src.index("export function queueStreamingComposerRequest")]

        assert "await res.json()" in submit
        assert "steerRecord.id" in submit
        assert "_trackSteeredBubble(sid, steerRecord.id, bubble)" in submit
        assert "/steer?limit=50" in lifecycle
        # Injection is terminal for this ephemeral composer chip, but it is
        # not called completion: only the durable server history can say it
        # reached the model, not that the agent acted on it.
        assert "bubble.remove()" in lifecycle
        assert "steerState === 'completed'" not in lifecycle

    def test_dashboard_polls_are_marked_as_polls(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        src = (root / "static" / "js" / "agentsDashboard.js").read_text(encoding="utf-8", errors="replace")
        assert "X-Odysseus-Poll" in src
        # The timer-driven reads must use it, or the dashboard resumes killing
        # the background runs it exists to display.
        assert "apiPoll('/api/agents/overview')" in src
        assert "apiPoll('/api/agents/approvals')" in src


class TestSteerIsObservable:
    """A queued steer used to leave no trace: the queue emptied whether the
    loop read the message or the turn ended and dropped it, so "did that
    correction land?" had no answer. Each message now carries an id and moves
    through states that are written to the activity feed, which is what makes
    the answer survive the turn."""

    def _states(self, session_id):
        from src import agent_control

        return [(row["id"], row["state"]) for row in agent_control.steer_history(session_id)]

    def test_queue_drain_and_injection_are_three_separate_facts(self):
        from src import agent_control

        rec = agent_control.steer("s", "use the staging database")
        assert rec["state"] == "queued" and rec["id"]
        assert self._states("s") == [(rec["id"], "queued")]

        # Draining is the running turn taking ownership — on its own it is not
        # proof the model was given anything.
        drained = agent_control.drain_steer_records("s", round_num=4)
        assert [r["state"] for r in drained] == ["acknowledged"]
        assert self._states("s") == [(rec["id"], "acknowledged")]

        agent_control.mark_injected(drained[0], round_num=4)
        history = agent_control.steer_history("s")
        assert history[0]["state"] == "injected"
        assert history[0]["round"] == 4
        # Every transition kept its own timestamp, which is what makes an age
        # ("queued 40s ago, still not picked up") possible after the fact.
        assert set(history[0]["timestamps"]) == {"queued", "acknowledged", "injected"}

    def test_completed_is_never_claimed(self):
        """Nothing observes a steering message being carried out, so nothing
        may say it was. A fabricated terminal state is worse than a missing
        one: it is the false assurance this whole change exists to remove."""
        from src import agent_control

        assert "completed" not in agent_control.STEER_STATES
        assert "superseded" not in agent_control.STEER_STATES
        rec = agent_control.steer("s", "stop and summarise")
        agent_control.mark_injected(agent_control.drain_steer_records("s")[0], round_num=1)
        agent_control.clear_steer("s")  # the turn ends after the injection
        row = agent_control.steer_history("s")[0]
        assert row["id"] == rec["id"]
        assert row["state"] == "injected", "turn end is not evidence the agent acted on it"

    def test_a_dropped_message_says_so_and_says_why(self):
        from src import agent_control

        agent_control.steer("s", "too late")
        assert agent_control.clear_steer("s") == ["too late"]
        row = agent_control.steer_history("s")[0]
        assert row["state"] == "cancelled"
        assert "turn ended" in row["reason"]

    def test_a_refused_message_is_recorded_against_the_target(self):
        """The sender gets an error; without this the target's own timeline
        never mentions that someone tried to correct it."""
        from src import agent_control

        agent_control.note_refused("s", "focus on the migration", "not queued: the chat was not running")
        row = agent_control.steer_history("s")[0]
        assert row["state"] == "failed" and row["reason"].startswith("not queued")
        assert agent_control.pending_steer("s") == []

    def test_a_full_queue_is_a_failure_with_a_reason_not_just_an_exception(self):
        from src import agent_control

        for i in range(agent_control._STEER_MAX):
            agent_control.steer("s", f"m{i}")
        with pytest.raises(ValueError):
            agent_control.steer("s", "one too many")
        refused = [row for row in agent_control.steer_history("s", limit=50) if row["state"] == "failed"]
        assert len(refused) == 1 and "queue full" in refused[0]["reason"]

    def test_history_outlives_the_in_memory_queue(self):
        """The queue is memory only. A process restart empties it, and a
        message left in it is not waiting for anything — steer_status says so
        rather than showing it as pending forever."""
        from src import agent_control

        agent_control.steer("s", "survives a restart")
        agent_control._STEER.clear()  # what a restart looks like from here
        status = agent_control.steer_status("s")
        assert status["queued"] == 0
        assert status["messages"][0]["state"] == "queued"
        assert status["messages"][0]["live"] is False

    def test_peer_messages_get_the_same_lifecycle(self):
        from src import agent_control

        agent_control.steer("s", "found the bug, don't also fix it", kind="peer",
                            from_session="a1", from_session_name="Parser fix")
        row = agent_control.steer_history("s")[0]
        assert row["kind"] == "peer" and row["from_session"] == "a1"
        assert row["state"] == "queued"
