"""A message sent mid-turn must not vanish.

Reported 2026-09-20: "if I send a message while the current chat is operating
on something it just deletes my message" / "it gets stuck steering then
disappears".

A mid-turn message is queued as a steer and drained at the TOP of a round. If
it lands while the final round is already running, nothing drains it again:
the turn ends, `clear_steer` cancels it, and the only trace is a
`steer_dropped` SSE event that no client listened for. The pending chip sat on
"Steering" until the next redraw removed it.

Two halves, both covered here:

* the loop extends a turn that would end with a steer still queued, so the
  message is actually read (the fork behaviour from `982e60eb`, lost in the
  2026-09-18 upstream sync);
* whatever still has to be dropped is reported with its id and full text, so
  the client can settle that chip and hand the text back instead of losing it.
"""

import os
import re

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _isolated_steer_queue(tmp_path, monkeypatch):
    from src import agent_activity, agent_control, constants

    # Queueing a steer writes to the activity feed, so relocate the data dir.
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    agent_activity._reset_for_tests()
    agent_control._STEER.clear()
    yield
    agent_control._STEER.clear()
    agent_activity._reset_for_tests()


# ── the drop path carries enough to recover the message ────────────────────

class TestDroppedSteerIsRecoverable:
    def test_records_carry_the_id_and_text(self):
        from src import agent_control

        agent_control.steer("s", "also check the archive", run_id="r1")
        dropped = agent_control.clear_steer_records("s", run_id="r1")

        assert len(dropped) == 1
        assert dropped[0]["text"] == "also check the archive"
        assert dropped[0]["id"], "a dropped message with no id cannot settle its chip"
        assert dropped[0]["state"] == "cancelled"

    def test_the_text_only_wrapper_still_behaves(self):
        """Several callers only append the strings; they must not change."""
        from src import agent_control

        agent_control.steer("s", "late arrival")
        assert agent_control.clear_steer("s") == ["late arrival"]
        assert agent_control.pending_steer("s") == []
        assert agent_control.clear_steer("s") == []

    def test_clearing_tolerates_no_session(self):
        from src import agent_control

        assert agent_control.clear_steer_records(None) == []
        assert agent_control.clear_steer_records("") == []

    def test_a_dropped_message_is_never_reported_as_injected(self):
        """"Cancelled" is the honest state; the model never saw this text."""
        from src import agent_control

        agent_control.steer("s", "one more thing", run_id="r1")
        [rec] = agent_control.clear_steer_records("s", run_id="r1")
        assert rec["state"] == "cancelled"
        assert "the turn ended before it was drained" in (rec.get("reason") or "")


# ── the loop extends rather than dropping in the first place ───────────────

class TestTurnExtendsForAPendingSteer:
    """The round loop is a single 3000-line generator that needs a live model
    endpoint to run, so these assert on the source of the decision site. The
    behaviour they protect is a `continue` that must sit between the last
    guard and the turn-ending `break`."""

    @staticmethod
    def _loop_source():
        with open(os.path.join(REPO_ROOT, "src", "agent_loop.py"), encoding="utf-8") as fh:
            return fh.read()

    def test_the_turn_ending_break_is_guarded_by_a_pending_steer_check(self):
        src = self._loop_source()
        idx = src.index("break  # no tools — done")
        window = src[idx - 900:idx]
        assert "pending_steer" in window, (
            "the turn-ending break must first check for a steer that landed "
            "during this round, or the message is cancelled unread"
        )
        assert "continue" in window

    def test_the_extension_is_bounded(self):
        src = self._loop_source()
        assert "_MAX_STEER_EXTENSIONS" in src
        assert re.search(r"_steer_extensions\s*<\s*_MAX_STEER_EXTENSIONS", src), (
            "an unbounded extension lets a client steering in a loop pin a run open"
        )
        assert re.search(r"_steer_extensions\s*=\s*0", src), "counter must reset per turn"

    def test_the_loop_reads_the_run_scoped_queue(self):
        """`pending_steer` without a run_id aggregates across the session, so a
        sibling run's queue would extend the wrong turn forever."""
        src = self._loop_source()
        idx = src.index("break  # no tools — done")
        window = src[idx - 900:idx]
        assert "run_id=steer_run_id" in window

    def test_pending_steer_is_run_scoped_and_empties_on_drain(self):
        """What the extension relies on: each extra round drains the whole
        queue, so extending is self-limiting rather than a spin."""
        from src import agent_control

        agent_control.steer("s", "first", run_id="r1")
        assert agent_control.pending_steer("s", run_id="r1")
        assert agent_control.pending_steer("s", run_id="other") == []

        agent_control.drain_steer_records("s", run_id="r1")
        assert agent_control.pending_steer("s", run_id="r1") == []


# ── the client must actually listen ────────────────────────────────────────

class TestClientHandlesTheDrop:
    @staticmethod
    def _chat_js():
        with open(os.path.join(REPO_ROOT, "static", "js", "chat.js"), encoding="utf-8") as fh:
            return fh.read()

    def test_steer_dropped_is_handled(self):
        """It was emitted by the server and listened for by nobody, which is
        why the message disappeared without a word."""
        src = self._chat_js()
        assert "_handleSteerDropped" in src
        assert src.count("json.type === 'steer_dropped'") >= 2, (
            "both the live reader and the resume/replay reader must settle it"
        )

    def test_the_dropped_text_goes_back_to_the_user(self):
        src = self._chat_js()
        start = src.index("function _handleSteerDropped")
        body = src[start:start + 2000]
        assert "uiModule.el('message')" in body, "the text must return to the composer"
        assert "showError" in body, "and the user must be told it was not read"

    def test_a_peer_agent_message_is_not_pushed_into_the_composer(self):
        src = self._chat_js()
        start = src.index("function _handleSteerDropped")
        body = src[start:start + 2000]
        assert "'peer'" in body
