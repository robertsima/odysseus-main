"""One line in, one destination out. Each destination is read back from the real store."""
import json
from datetime import datetime



async def test_appointment_with_a_time_lands_on_the_calendar(cap, env, events):
    """Production bug: 'lunch with Sara tomorrow 1pm' filed as a task, so nothing blocked the hour."""
    res = await cap("admin", "Lunch with Sara tomorrow 1pm")

    assert (res["status"], res["destination"]) == ("created", "calendar")
    assert events("admin") == [("Lunch with Sara", datetime(2026, 10, 11, 13, 0), False)]
    assert env.td.calls == [] and env.model.prompts == []


async def test_dated_task_goes_to_todoist_with_an_explicit_due(cap, env):
    """Production bug: the date phrase stayed in the title and Todoist parsed it a second time."""
    res = await cap("admin", "pay the electricity bill friday")

    assert (res["status"], res["destination"]) == ("created", "todoist")
    assert env.td.added() == [["task", "add", "Pay the electricity bill", "--due", "2026-10-16", "--json"]]
    assert res["ref"] == env.td.tasks[0]["id"]


async def test_an_undated_task_is_created_undated_without_asking(cap, env):
    """A missing date on a plain task is not ambiguous."""
    res = await cap("admin", "buy printer paper")

    assert res["status"] == "created"
    assert env.td.added() == [["task", "add", "Buy printer paper", "--json"]]


async def test_a_timed_reminder_becomes_a_note_so_the_ping_loop_delivers_it(cap, env):
    """Production bug: 'remind me ... at 5pm' went to Todoist, which Odysseus never pings from."""
    res = await cap("admin", "remind me to call mom at 5pm")

    assert (res["status"], res["destination"]) == ("created", "note")
    [note] = env.notes.notes.values()
    assert note.title == "Call mom" and note.due_date.startswith("2026-10-10T17:00:00")
    assert env.td.calls == []


async def test_waiting_on_becomes_a_waiting_outcome_with_a_linked_reminder(cap, env):
    res = await cap("admin", "waiting on Sam for the signed contract, follow up friday")

    assert (res["status"], res["destination"]) == ("created", "outcome")
    out = env.outcomes.get("admin", res["outcome_id"])
    assert (out["state"], out["waiting_on"], out["source"]) == ("waiting", "Sam", "capture")
    assert out["follow_up_at"].startswith("2026-10-16T09:00")
    [link] = [x for x in out["links"] if x["kind"] == "note"]
    note = env.notes.notes[link["ref"]]
    assert note.due_date.startswith("2026-10-16T09:00")
    assert env.td.calls == []


async def test_waiting_without_a_date_gets_a_default_follow_up_and_says_so(cap, env):
    res = await cap("admin", "waiting for the plumber to call back")

    out = env.outcomes.get("admin", res["outcome_id"])
    assert out["state"] == "waiting" and out["follow_up_at"]
    assert res["follow_up_defaulted"] is True


async def test_a_multi_step_goal_becomes_a_project_outcome(cap, env):
    res = await cap("admin", "plan the offsite: book the venue, then send invites, then collect rsvps")

    assert (res["status"], res["destination"]) == ("created", "outcome")
    out = env.outcomes.get("admin", res["outcome_id"])
    assert out["kind"] == "project" and out["state"] == "open" and out["source"] == "capture"
    assert env.td.calls == []


async def test_a_todoist_capture_from_a_multi_step_sentence_is_linked_to_an_outcome(cap, env):
    text = "build the importer: parse the file, then validate rows, then load them"
    res = await cap("admin", text, destination="todoist")

    assert res["destination"] == "todoist" and res["outcome_id"]
    [out] = env.outcomes.find_by_link("admin", "todoist_task", res["ref"])
    assert out["id"] == res["outcome_id"] and out["kind"] == "project"


async def test_the_model_decides_only_when_the_rules_cannot(cap, env):
    env.model.replies.append(json.dumps({"destination": "task", "confidence": 0.9}))

    res = await cap("admin", "kombucha starter")

    assert (res["status"], res["destination"]) == ("created", "todoist")
    assert len(env.model.prompts) == 1


async def test_with_no_model_an_unclear_line_is_still_captured_as_a_task(cap, env, monkeypatch):
    import src.endpoint_resolver as resolver
    monkeypatch.setattr(resolver, "resolve_endpoint", lambda *a, **k: ("", "", {}))

    res = await cap("admin", "kombucha starter")

    assert res["status"] == "created" and env.td.creates == 1


async def test_the_person_timezone_is_applied_to_the_due_time(cap, env):
    res = await cap("alice", "remind me to stretch tomorrow at 9am", tz_offset=540)

    [note] = env.notes.notes.values()
    assert note.due_date == "2026-10-11T09:00:00+09:00" and res["due"] == note.due_date
