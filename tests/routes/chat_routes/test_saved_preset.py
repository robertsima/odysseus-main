"""A chat's saved preset outlives turns that carry none.

Bug (2026-10-10): the per-turn settings write stored `preset_id or None`, so one
turn from a client that sent no preset erased the preset the chat had been saved with.
"""
from tests.routes.chat_routes.agent_turn import agent_turn  # noqa: F401  (fixture)


def _saved(turn):
    return turn.client.get(f"/api/session/{turn.session_id}/settings").json()["settings"].get("preset_id")


def test_a_turn_without_a_preset_keeps_the_saved_one(agent_turn):
    agent_turn.client.patch(f"/api/session/{agent_turn.session_id}/settings", json={"preset_id": "code_analyze"})

    agent_turn.send({"message": "list the files here", "mode": "agent"})

    assert _saved(agent_turn) == "code_analyze"


def test_a_preset_on_a_turn_replaces_the_saved_one(agent_turn):
    agent_turn.client.patch(f"/api/session/{agent_turn.session_id}/settings", json={"preset_id": "code_analyze"})

    agent_turn.send({"message": "list the files here", "mode": "agent", "preset_id": "brainstorm"})

    assert _saved(agent_turn) == "brainstorm"


def test_clearing_stays_with_the_settings_patch(agent_turn):
    agent_turn.client.patch(f"/api/session/{agent_turn.session_id}/settings", json={"preset_id": "code_analyze"})
    agent_turn.client.patch(f"/api/session/{agent_turn.session_id}/settings", json={"preset_id": None})

    agent_turn.send({"message": "list the files here", "mode": "agent"})

    assert not _saved(agent_turn)
