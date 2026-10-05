"""A message sent mid-turn must not vanish.

Reported 2026-09-20: "if I send a message while the current chat is operating
on something it just deletes my message" / "it gets stuck steering then
disappears".

A mid-turn message is queued as a steer and drained at the TOP of a round. If
it lands while the final round is already running, nothing drains it again:
the turn ends, `clear_steer` cancels it, and the only trace is a
`steer_dropped` SSE event that no client listened for. The pending chip sat on
"Steering" until the next redraw removed it.

Two halves; the loop half is in tests/src/agent_loop/test_steer_during_final_round.py
and the client half in tests/static/js/chat/steer_dropped.test.mjs:

* the loop extends a turn that would end with a steer still queued, so the
  message is actually read (the fork behaviour from `982e60eb`, lost in the
  2026-09-18 upstream sync);
* whatever still has to be dropped is reported with its id and full text, so
  the client can settle that chip and hand the text back instead of losing it.
"""

import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


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
    """The loop side is tests/src/agent_loop/test_steer_during_final_round.py."""

    def test_pending_steer_is_run_scoped_and_empties_on_drain(self):
        """What the extension relies on: each extra round drains the whole
        queue, so extending is self-limiting rather than a spin."""
        from src import agent_control

        agent_control.steer("s", "first", run_id="r1")
        assert agent_control.pending_steer("s", run_id="r1")
        assert agent_control.pending_steer("s", run_id="other") == []

        agent_control.drain_steer_records("s", run_id="r1")
        assert agent_control.pending_steer("s", run_id="r1") == []
