"""Today ranks what needs the person now and says why."""
from datetime import datetime

import src.database
from core.database import CalendarCal, CalendarEvent

from .conftest import NOW, epoch, interrupted_run, keys, outcome, person_blocker, task


def _event(owner, uid, summary, start, *, all_day=False, calendar_id=None, origin=None):
    db = src.database.SessionLocal()
    try:
        cal_id = calendar_id or f"cal-{owner}"
        if not db.query(CalendarCal).filter(CalendarCal.id == cal_id).first():
            db.add(CalendarCal(id=cal_id, owner=owner, name="Personal"))
        db.add(CalendarEvent(uid=uid, calendar_id=cal_id, summary=summary, dtstart=start,
                             dtend=start.replace(hour=min(start.hour + 1, 23)), all_day=all_day,
                             is_utc=True, origin=origin))
        db.commit()
    finally:
        db.close()


def _note(client, title, due, **extra):
    res = client.post("/api/notes", json={"title": title, "due_date": due, **extra})
    assert res.status_code == 200, res.text
    return res.json()["id"]


def test_today_orders_overdue_then_due_today_then_blockers_then_follow_ups(world):
    """Production bug: a person-blocking blocker or a quiet follow-up outranked a task that was already late."""
    admin = world.api.as_admin()
    world.td.tasks = [
        task("t-today", "Call the dentist", "2026-10-10", priority=2),
        task("t-late", "Pay rent", "2026-10-06", priority=4),
    ]
    _event("admin", "ev-1", "Standup", datetime(2026, 10, 10, 14, 0))
    note_id = _note(admin, "Water the plants", "2026-10-10T16:00:00Z")
    blocked = outcome("admin", "Ship the importer", state="active", blocker=person_blocker())
    waiting = outcome("admin", "Contract from Dana", state="waiting", waiting_on="Dana",
                      follow_up_at="2026-10-10T08:00:00")

    got = world.get(admin)

    assert keys(got["today"]) == [
        "todoist:t-late", "todoist:t-today", "event:ev-1", f"note:{note_id}",
        f"outcome:{blocked}",
    ]
    assert keys(got["next"])[0] == f"outcome:{waiting}"


def test_every_item_says_why_it_is_there(world):
    admin = world.api.as_admin()
    world.td.tasks = [task("t-today", "Call the dentist", "2026-10-10T15:00:00Z"),
                      task("t-late", "Pay rent", "2026-10-06"),
                      task("t-allday", "Water filter", "2026-10-10")]
    _event("admin", "ev-1", "Standup", datetime(2026, 10, 10, 14, 0))
    note_id = _note(admin, "Water the plants", "2026-10-10T16:00:00Z")
    blocked = outcome("admin", "Ship the importer", state="active", blocker=person_blocker())

    got = world.get(admin)

    why = {item["key"]: item["why"] for item in got["today"] + got["next"]}
    assert why["todoist:t-late"] == "Overdue since Tue"
    assert why["todoist:t-today"] == "Due today at 3:00 PM"
    assert why["todoist:t-allday"] == "Due today"
    assert why["event:ev-1"] == "On your calendar at 2:00 PM"
    assert why[f"note:{note_id}"] == "Reminder due today at 4:00 PM"
    assert why[f"outcome:{blocked}"] == "Blocked, waiting on you"


def test_outcome_blocker_and_source_label_are_carried_on_the_item(world):
    alice = world.api.as_user("alice")
    blocked = outcome("alice", "Ship the importer", state="active", blocker=person_blocker("approval"))

    got = world.get(alice)

    item = got["today"][0]
    assert item["key"] == f"outcome:{blocked}" and item["source"] == "outcome"
    assert item["why"] == "Blocked, waiting for your approval"
    assert item["blocker"]["user_action"] == "Paste the staging deploy key into note 'deploy'."
    assert item["blocker"]["summary"] == "The worker needs input from you."
    assert "done" in item["actions"] and "defer" in item["actions"]


def test_next_lists_active_work_resumable_runs_and_open_outcomes_by_priority(world):
    """Production bug: a priority-1 outcome sat below a priority-less one because only recency was used."""
    alice = world.api.as_user("alice")
    low = outcome("alice", "Sort photos")
    high = outcome("alice", "Plan the trip", priority=1)
    active = outcome("alice", "Write the spec", state="active", next_action="Draft section 2")
    run = interrupted_run("alice", "w1", epoch(NOW) - 2 * 3600)

    got = world.get(alice)

    assert got["today"] == []
    assert keys(got["next"]) == [f"outcome:{high}", f"outcome:{active}", f"run:{run}"]
    why = {item["key"]: item["why"] for item in got["next"]}
    assert why[f"outcome:{high}"] == "Not started, priority 1"
    assert why[f"outcome:{active}"] == "In progress. Next step: Draft section 2"
    assert why[f"run:{run}"] == "Interrupted 2 hours ago"
    assert f"outcome:{low}" not in keys(got["next"])  # the cap of three


def test_runs_that_finished_normally_or_long_ago_are_not_offered(world):
    alice = world.api.as_user("alice")
    interrupted_run("alice", "w1", epoch(NOW) - 8 * 86400)
    interrupted_run("alice", "w2", epoch(NOW) - 3600, status="completed")
    partial = interrupted_run("alice", "w3", epoch(NOW) - 3600, status="partial")

    got = world.get(alice)

    assert keys(got["today"] + got["next"]) == [f"run:{partial}"]


def test_today_is_capped_at_five_and_next_at_three_with_overflow_moving_down(world):
    admin = world.api.as_admin()
    world.td.tasks = [task(f"t{n}", f"Task {n}", f"2026-10-0{n}") for n in range(1, 8)]
    extra = [outcome("admin", f"Open {n}", priority=n) for n in (1, 2, 3, 4)]

    got = world.get(admin)

    assert len(got["today"]) == 5 and len(got["next"]) == 3
    assert keys(got["today"]) == [f"todoist:t{n}" for n in range(1, 6)]
    assert keys(got["next"]) == ["todoist:t6", "todoist:t7", f"outcome:{extra[0]}"]


def test_a_snoozed_item_is_hidden_until_its_time(world):
    alice = world.api.as_user("alice")
    later = outcome("alice", "Renew passport", priority=1, snoozed_until="2026-10-11T09:00:00")

    hidden = world.get(alice)
    world.clock["now"] = datetime(2026, 10, 11, 9, 0, 1)
    shown = world.get(alice)

    assert hidden["today"] == [] and hidden["next"] == []
    assert keys(shown["next"]) == [f"outcome:{later}"]


def test_a_waiting_outcome_surfaces_only_once_its_follow_up_is_due(world):
    alice = world.api.as_user("alice")
    soon = outcome("alice", "Reply from Dana", state="waiting", waiting_on="Dana",
                   follow_up_at="2026-10-14T09:00:00")

    before = world.get(alice)
    world.clock["now"] = datetime(2026, 10, 14, 9, 5)
    after = world.get(alice)

    assert before["today"] == [] and before["next"] == []
    assert keys(after["today"]) == [f"outcome:{soon}"]
    assert after["today"][0]["why"] == "Waiting on Dana, follow-up due today"
    assert after["today"][0]["due"] is not None


def test_an_outcome_linked_to_a_todoist_task_appears_once_as_the_outcome(world):
    """Production bug: the same errand showed twice, once from Todoist and once from the outcome."""
    admin = world.api.as_admin()
    world.td.tasks = [task("t-late", "Pay rent (Todoist wording)", "2026-10-06")]
    linked = outcome("admin", "Pay the March rent", state="active", links=[("todoist_task", "t-late")])

    got = world.get(admin)

    assert keys(got["today"]) == [f"outcome:{linked}"]
    assert got["today"][0]["title"] == "Pay the March rent"
    assert got["today"][0]["why"] == "Overdue since Tue"
    assert got["next"] == []


def test_a_closed_outcome_does_not_hide_its_open_todoist_task(world):
    admin = world.api.as_admin()
    world.td.tasks = [task("t-late", "Pay rent", "2026-10-06")]
    done = outcome("admin", "Pay the rent", links=[("todoist_task", "t-late")])
    res = admin.post(f"/api/outcomes/{done}/state", json={"state": "done", "confirm": True})
    assert res.status_code == 200

    got = world.get(admin)

    assert keys(got["today"]) == ["todoist:t-late"]


def test_the_todoist_mirror_calendar_is_not_counted_twice(world):
    admin = world.api.as_admin()
    world.td.tasks = [task("t-today", "Call the dentist", "2026-10-10")]
    _event("admin", "todoist-task-t-today", "Call the dentist", datetime(2026, 10, 10), all_day=True,
           calendar_id="todoist-cal", origin="todoist")

    got = world.get(admin)

    assert keys(got["today"]) == ["todoist:t-today"]


def test_todoist_priority_orders_items_due_the_same_day(world):
    admin = world.api.as_admin()
    world.td.tasks = [task("low", "Low", "2026-10-10", priority=1), task("top", "Top", "2026-10-10", priority=4)]

    got = world.get(admin)

    assert keys(got["today"]) == ["todoist:top", "todoist:low"]


def test_a_run_that_was_picked_up_again_is_no_longer_offered(world):
    """Production bug: an interrupted run kept being offered after the worker had been resumed and finished."""
    alice = world.api.as_user("alice")
    interrupted_run("alice", "w1", epoch(NOW) - 7200)
    interrupted_run("alice", "w1", epoch(NOW) - 3600, status="completed")

    got = world.get(alice)

    assert got["today"] == [] and got["next"] == []
