"""Correcting a capture: re-route within 30 minutes, remove only what this capture made."""
from datetime import timedelta


from src import capture


async def test_moving_a_todoist_task_to_a_note_removes_the_task_and_keeps_one_item(cap, env):
    """Production bug: a mis-routed capture left the person to delete the wrong copy by hand."""
    first = await cap("admin", "pay the electricity bill friday", idempotency_key="m1")

    moved = await capture.move("admin", "m1", "note")

    assert (moved["status"], moved["destination"]) == ("created", "note")
    assert env.td.deletes == [first["ref"]] and env.td.tasks == []
    [note] = env.notes.notes.values()
    assert note.title == "Pay the electricity bill" and note.due_date.startswith("2026-10-16")
    assert moved["moved_from"] == {"destination": "todoist", "ref": first["ref"], "removed": True}


async def test_moving_a_calendar_event_to_a_task_deletes_the_event(cap, env, events):
    await cap("admin", "Lunch with Sara tomorrow 1pm", idempotency_key="m2")

    moved = await capture.move("admin", "m2", "todoist")

    assert moved["destination"] == "todoist" and events("admin") == []
    assert env.td.creates == 1


async def test_a_moved_capture_replays_as_the_new_destination(cap, env):
    await cap("admin", "pay the electricity bill friday", idempotency_key="m3")
    moved = await capture.move("admin", "m3", "note")

    replay = await cap("admin", "pay the electricity bill friday", idempotency_key="m3")

    assert replay["destination"] == "note" and replay["ref"] == moved["ref"]
    assert env.td.creates == 1


async def test_a_duplicate_result_is_never_moved_or_removed(cap, env):
    """Production bug: moving a capture that matched an existing task deleted the person's own task."""
    seeded = env.td.seed("Pay the electricity bill")
    dup = await cap("admin", "pay the electricity bill", idempotency_key="m4", destination="todoist")
    assert dup["status"] == "duplicate"

    moved = await capture.move("admin", "m4", "note")

    assert moved["status"] == "refused" and moved["code"] == "duplicate"
    assert env.td.deletes == [] and [t["id"] for t in env.td.tasks] == [seeded["id"]]
    assert env.notes.notes == {}


async def test_after_thirty_minutes_the_move_is_refused(cap, env):
    first = await cap("admin", "pay the electricity bill friday", idempotency_key="m5")
    env.clock.now += timedelta(minutes=31)

    moved = await capture.move("admin", "m5", "note")

    assert moved["status"] == "refused" and moved["code"] == "too_late"
    assert env.td.deletes == [] and env.notes.notes == {} and env.td.tasks[0]["id"] == first["ref"]


async def test_just_inside_thirty_minutes_still_moves(cap, env):
    await cap("admin", "pay the electricity bill friday", idempotency_key="m6")
    env.clock.now += timedelta(minutes=29, seconds=59)

    assert (await capture.move("admin", "m6", "note"))["status"] == "created"


async def test_a_failed_new_destination_leaves_the_original_in_place(cap, env, monkeypatch):
    first = await cap("admin", "pay the electricity bill friday", idempotency_key="m7")
    from src.tools import notes as notes_tool

    async def broken(content, owner=None, allow_private=False):
        return {"error": "disk full", "exit_code": 1}

    monkeypatch.setattr(notes_tool, "do_manage_notes", broken)

    moved = await capture.move("admin", "m7", "note")

    assert moved["status"] == "failed"
    assert env.td.deletes == [] and env.td.tasks[0]["id"] == first["ref"]
    again = await cap("admin", "pay the electricity bill friday", idempotency_key="m7")
    assert again["ref"] == first["ref"] and again["destination"] == "todoist"


async def test_moving_a_waiting_capture_drops_its_outcome_and_reminder(cap, env):
    first = await cap("alice", "waiting on Sam for the contract", idempotency_key="m8")
    assert len(env.notes.notes) == 1

    moved = await capture.move("alice", "m8", "note")

    assert moved["destination"] == "note"
    assert env.outcomes.get("alice", first["outcome_id"])["state"] == "dropped"
    assert [n.title for n in env.notes.notes.values()] == ["Waiting on Sam for the contract"]


async def test_moving_to_where_it_already_is_is_refused(cap, env):
    await cap("admin", "pay the electricity bill friday", idempotency_key="m9")

    assert (await capture.move("admin", "m9", "todoist"))["code"] == "invalid"


async def test_an_unknown_key_is_not_found(env):
    assert (await capture.move("admin", "nope", "note"))["code"] == "not_found"
