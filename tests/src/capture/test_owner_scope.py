"""Keys, replays and moves are per owner; a non-admin never reaches Todoist."""
import pytest

from src import capture


@pytest.mark.security
async def test_another_owner_cannot_replay_or_move_a_capture(cap, env):
    """Production bug: a key looked up without the owner would hand Bob's result to Alice."""
    bob = await cap("bob", "remind me to call mom at 5pm", idempotency_key="shared-key")

    alice = await cap("alice", "remind me to water the plants at 6pm", idempotency_key="shared-key")

    assert alice["ref"] != bob["ref"] and alice.get("replayed") is None
    assert env.notes.find(bob["ref"], "bob") is not None
    assert env.notes.find(alice["ref"], "alice") is not None


@pytest.mark.security
async def test_move_cannot_touch_a_key_the_caller_never_made(cap, env):
    bob = await cap("bob", "remind me to call mom at 5pm", idempotency_key="bobs-key")

    moved = await capture.move("alice", "bobs-key", "calendar")

    assert moved["status"] == "refused" and moved["code"] == "not_found"
    assert env.notes.find(bob["ref"], "bob") is not None


@pytest.mark.security
async def test_a_non_admin_task_becomes_a_note_and_never_calls_td(cap, env):
    res = await cap("alice", "pay the electricity bill friday")

    assert (res["status"], res["destination"]) == ("created", "note")
    assert res["fallback_from"] == "todoist" and "Todoist" in res["message"]
    assert env.td.calls == []
    [note] = env.notes.notes.values()
    assert note.owner == "alice" and note.due_date.startswith("2026-10-16")


@pytest.mark.security
async def test_a_non_admin_cannot_force_todoist_through_the_destination_argument(cap, env):
    res = await cap("alice", "pay the electricity bill friday", destination="todoist")

    assert res["destination"] == "note" and env.td.calls == []


@pytest.mark.security
async def test_a_non_admin_cannot_move_a_capture_into_todoist(cap, env):
    await cap("alice", "pay the electricity bill friday", idempotency_key="k")

    moved = await capture.move("alice", "k", "todoist")

    assert env.td.calls == [] and moved.get("destination") != "todoist"
