"""agent_mailbox is peer-to-peer messaging built on top of agent_control's
existing steer queue (see both modules' docstrings): a peer message is a
steer record addressed by one running session to another instead of typed by
a human, so it drains through the exact same agent-loop code path with no new
plumbing there.

That reuse means two things must both hold, and are tested here:

1. The primitive itself (steer/drain_steer/pending_steer/clear_steer) must
   keep behaving exactly as it did before agent_mailbox started using it —
   nothing here is a rewrite, only additive optional fields.
2. A peer message must be unmistakably NOT a human steer once it is sitting
   in that same queue, because agent_loop.py wraps every drained record with
   "[Mid-task instruction from the user]" regardless of who queued it. A
   model that mistook a peer's message for its user's instruction would
   change who it thinks it is working for.

No network, no real sessions/session manager: agent_mailbox's one seam onto
real session storage (`_session_owner`) and its one seam onto settings
(`get_setting`) are monkeypatched directly.
"""

import pytest

from src import agent_control, agent_mailbox


@pytest.fixture(autouse=True)
def _clean_state():
    agent_control._STEER.clear()
    agent_mailbox._SEND_COUNTS.clear()
    yield
    agent_control._STEER.clear()
    agent_mailbox._SEND_COUNTS.clear()


def _settings(peer_messaging=True, budget=8):
    """A fake get_setting() covering only the two keys agent_mailbox reads."""
    values = {"agent_peer_messaging": peer_messaging, "agent_peer_message_budget": budget}

    def _get(key, default=None):
        return values.get(key, default)

    return _get


class TestPeerMessageDrainsThroughSteer:
    def test_send_lands_in_the_target_steer_queue(self, monkeypatch):
        monkeypatch.setattr(agent_mailbox, "get_setting", _settings())

        result = agent_mailbox.send("target", "stop editing config.py, I already did", from_session="sender")

        assert result["ok"] is True
        assert result["to_session"] == "target"
        # It is sitting in the SAME queue agent_control.pending_steer/drain_steer
        # already serve to the agent loop and the dashboard.
        assert len(agent_control.pending_steer("target")) == 1
        drained = agent_control.drain_steer("target")
        assert len(drained) == 1
        assert "stop editing config.py, I already did" in drained[0]
        # Draining empties it exactly like a human steer would.
        assert agent_control.drain_steer("target") == []

    def test_inbox_and_pending_are_read_only_views(self, monkeypatch):
        monkeypatch.setattr(agent_mailbox, "get_setting", _settings())

        agent_mailbox.send("target", "first", from_session="a")
        agent_mailbox.send("target", "second", from_session="a")

        assert agent_mailbox.pending("target") == 2
        inbox = agent_mailbox.inbox("target")
        assert [rec["kind"] for rec in inbox] == ["peer", "peer"]
        # Read-only: reading the inbox must not drain the queue the agent
        # loop still needs to consume.
        assert agent_mailbox.pending("target") == 2
        assert len(agent_control.pending_steer("target")) == 2

    def test_inbox_excludes_plain_human_steers(self, monkeypatch):
        monkeypatch.setattr(agent_mailbox, "get_setting", _settings())

        agent_control.steer("target", "a human correction from the dashboard")
        agent_mailbox.send("target", "a peer message", from_session="a")

        # Both share the queue, but the peer-message inbox view is scoped to
        # kind=="peer" only -- a human's own steer must never show up there.
        assert agent_mailbox.pending("target") == 1
        assert agent_control.pending_steer("target").__len__() == 2


class TestPrefixIsDistinguishableFromAHumanSteer:
    def test_peer_text_is_tagged_and_named(self, monkeypatch):
        monkeypatch.setattr(agent_mailbox, "get_setting", _settings())

        agent_control.steer("human-target", "please also check the tests")
        agent_mailbox.send("peer-target", "please also check the tests", from_session="sender-1",
                           from_session_name="Refactor bot")

        human_text = agent_control.drain_steer("human-target")[0]
        peer_text = agent_control.drain_steer("peer-target")[0]

        # Identical payload text, but the peer version is wrapped with an
        # explicit, unmistakable tag naming the sending session -- the human
        # steer carries no such tag at this layer (agent_loop.py's shared
        # "[Mid-task instruction from the user]" wrapper is applied later, to
        # both alike, which is exactly why the distinguishing tag has to live
        # in the text itself rather than rely on that wrapper).
        assert human_text == "please also check the tests"
        assert "PEER AGENT" in peer_text
        assert "sender-1" in peer_text
        assert "Refactor bot" in peer_text
        assert peer_text != human_text

    def test_pending_steer_record_carries_kind_and_sender(self, monkeypatch):
        monkeypatch.setattr(agent_mailbox, "get_setting", _settings())

        agent_control.steer("s", "human note")
        agent_mailbox.send("s", "peer note", from_session="sender-2")

        recs = agent_control.pending_steer("s")
        kinds = {r.get("from_session"): r["kind"] for r in recs}
        assert kinds[None] == "user"
        assert kinds["sender-2"] == "peer"


class TestBudget:
    def test_exhaustion_is_refused_not_raised(self, monkeypatch):
        monkeypatch.setattr(agent_mailbox, "get_setting", _settings(budget=2))

        first = agent_mailbox.send("t", "one", from_session="s")
        second = agent_mailbox.send("t", "two", from_session="s")
        third = agent_mailbox.send("t", "three", from_session="s")

        assert first["ok"] and second["ok"]
        assert third["ok"] is False
        assert "budget" in third["reason"].lower()
        # The third message must not have been queued.
        assert len(agent_control.pending_steer("t")) == 2

    def test_budget_is_per_sender(self, monkeypatch):
        monkeypatch.setattr(agent_mailbox, "get_setting", _settings(budget=1))

        assert agent_mailbox.send("t", "from s1", from_session="s1")["ok"] is True
        assert agent_mailbox.send("t", "from s2", from_session="s2")["ok"] is True
        assert agent_mailbox.send("t", "again from s1", from_session="s1")["ok"] is False


class TestRefusals:
    def test_self_send_is_refused(self, monkeypatch):
        monkeypatch.setattr(agent_mailbox, "get_setting", _settings())

        result = agent_mailbox.send("same", "hello", from_session="same")

        assert result["ok"] is False
        assert "itself" in result["reason"]
        assert agent_control.pending_steer("same") == []

    def test_cross_owner_send_is_refused(self, monkeypatch):
        monkeypatch.setattr(agent_mailbox, "get_setting", _settings())
        monkeypatch.setattr(agent_mailbox, "_session_owner", lambda sid: "someone-else")

        result = agent_mailbox.send("their-session", "hi", from_session="mine", owner="me")

        assert result["ok"] is False
        assert "not found" in result["reason"]
        assert agent_control.pending_steer("their-session") == []

    def test_matching_owner_is_allowed(self, monkeypatch):
        monkeypatch.setattr(agent_mailbox, "get_setting", _settings())
        monkeypatch.setattr(agent_mailbox, "_session_owner", lambda sid: "me")

        result = agent_mailbox.send("their-session", "hi", from_session="mine", owner="me")

        assert result["ok"] is True

    def test_unowned_caller_skips_owner_check(self, monkeypatch):
        """owner=None (auth disabled / legacy) behaves like send_to_session: no
        cross-owner check is possible, so none is applied."""
        monkeypatch.setattr(agent_mailbox, "get_setting", _settings())
        monkeypatch.setattr(agent_mailbox, "_session_owner", lambda sid: "anybody")

        result = agent_mailbox.send("their-session", "hi", from_session="mine", owner=None)

        assert result["ok"] is True

    def test_queue_depth_cap_surfaces_clean_error(self, monkeypatch):
        """agent_control._STEER_MAX caps the recipient's queue depth regardless
        of who is sending; a peer message must respect it and refuse cleanly,
        never raise out of send()."""
        monkeypatch.setattr(agent_mailbox, "get_setting", _settings(budget=1000))

        for i in range(agent_control._STEER_MAX):
            agent_control.steer("full", f"filler {i}")

        result = agent_mailbox.send("full", "one more", from_session="s")

        assert result["ok"] is False
        assert result["reason"]
        assert len(agent_control.pending_steer("full")) == agent_control._STEER_MAX

    def test_peer_messaging_disabled_refuses_with_reason(self, monkeypatch):
        monkeypatch.setattr(agent_mailbox, "get_setting", _settings(peer_messaging=False))

        result = agent_mailbox.send("t", "hello", from_session="s")

        assert result["ok"] is False
        assert "off" in result["reason"].lower()
        assert agent_control.pending_steer("t") == []

    def test_empty_message_refused(self, monkeypatch):
        monkeypatch.setattr(agent_mailbox, "get_setting", _settings())

        result = agent_mailbox.send("t", "   ", from_session="s")

        assert result["ok"] is False


class TestExistingSteerBehaviourUnchanged:
    """agent_mailbox is additive: the primitive it's built on must still work
    exactly as every other caller (the Agents dashboard, agent_loop.py, and
    tests/test_agent_turn_lifecycle.py) already relies on."""

    def test_plain_steer_round_trip(self):
        agent_control.steer("s", "first")
        agent_control.steer("s", "second")
        assert agent_control.drain_steer("s") == ["first", "second"]
        assert agent_control.drain_steer("s") == []

    def test_plain_steer_default_kind_is_user(self):
        rec = agent_control.steer("s", "hello")
        assert rec["kind"] == "user"
        assert "from_session" not in rec

    def test_clear_steer_returns_and_empties(self):
        agent_control.steer("s", "late arrival")
        assert agent_control.clear_steer("s") == ["late arrival"]
        assert agent_control.pending_steer("s") == []
        assert agent_control.clear_steer("s") == []

    def test_empty_text_still_rejected(self):
        with pytest.raises(ValueError):
            agent_control.steer("s", "   ")

    def test_queue_still_bounded(self):
        for i in range(agent_control._STEER_MAX):
            agent_control.steer("s", f"m{i}")
        with pytest.raises(ValueError):
            agent_control.steer("s", "one too many")

    def test_drain_steer_records_keeps_metadata(self, monkeypatch):
        """New helper for the eventual agent_loop.py hook: unlike drain_steer,
        it hands back the full record so a caller can branch on `kind`."""
        monkeypatch.setattr(agent_mailbox, "get_setting", _settings())
        agent_control.steer("s", "human note")
        agent_mailbox.send("s", "peer note", from_session="sender")

        records = agent_control.drain_steer_records("s")

        assert [r["kind"] for r in records] == ["user", "peer"]
        assert agent_control.drain_steer_records("s") == []


def test_message_agent_native_call_reaches_execution_pipeline():
    """A schema and handler are useless if TOOL_TAGS rejects the native call."""
    from src.tool_schemas import function_call_to_tool_block

    block = function_call_to_tool_block(
        "message_agent", '{"session_id":"peer","message":"status?"}'
    )
    assert block is not None
    assert block.tool_type == "message_agent"
