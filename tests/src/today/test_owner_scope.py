"""Alice's Today never contains bob's outcomes, runs, notes or calendar events."""
from datetime import datetime

import pytest

import src.database
from core.database import CalendarCal, CalendarEvent
from src.notes_markdown import NoteRecord
from src.notes_store import STORE

from .conftest import NOW, epoch, interrupted_run, keys, outcome, person_blocker

pytestmark = pytest.mark.security


def _event(owner, uid, summary):
    db = src.database.SessionLocal()
    try:
        cal_id = f"cal-{owner}"
        if not db.query(CalendarCal).filter(CalendarCal.id == cal_id).first():
            db.add(CalendarCal(id=cal_id, owner=owner, name="Personal"))
        db.add(CalendarEvent(uid=uid, calendar_id=cal_id, summary=summary, dtstart=datetime(2026, 10, 10, 14),
                             dtend=datetime(2026, 10, 10, 15), is_utc=True))
        db.commit()
    finally:
        db.close()


def _seed_everything(owner, label):
    ids = {}
    ids["blocked"] = outcome(owner, f"{label} blocked", state="active", blocker=person_blocker())
    ids["active"] = outcome(owner, f"{label} active", state="active")
    ids["run"] = interrupted_run(owner, f"w-{label}", epoch(NOW) - 3600)
    _event(owner, f"ev-{label}", f"{label} meeting")
    note = NoteRecord(id=f"note-{label}", owner=owner, title=f"{label} note", due_date="2026-10-10T16:00:00Z")
    STORE.save(note, enforce_readonly=False)
    return ids


def test_alice_sees_only_her_own_items(world):
    """Production bug: a read that filtered `owner in (alice, None)` or skipped the filter leaked bob's work."""
    alice, bob = world.api.as_user("alice"), world.api.as_user("bob")
    mine = _seed_everything("alice", "alice")
    theirs = _seed_everything("bob", "bob")

    got_alice, got_bob = world.get(alice), world.get(bob)

    alice_keys = keys(got_alice["today"] + got_alice["next"])
    bob_keys = keys(got_bob["today"] + got_bob["next"])
    assert set(alice_keys) == {f"outcome:{mine['blocked']}", f"outcome:{mine['active']}", f"run:{mine['run']}",
                               "event:ev-alice", "note:note-alice"}
    assert set(bob_keys) == {f"outcome:{theirs['blocked']}", f"outcome:{theirs['active']}",
                             f"run:{theirs['run']}", "event:ev-bob", "note:note-bob"}


def test_rows_with_no_owner_belong_to_nobody(world):
    alice = world.api.as_user("alice")
    interrupted_run(None, "w-legacy", epoch(NOW) - 3600)
    _event(None, "ev-legacy", "legacy meeting")
    STORE.save(NoteRecord(id="note-legacy", owner=None, title="legacy note", due_date="2026-10-10T16:00:00Z"),
               enforce_readonly=False)

    got = world.get(alice)

    assert got["today"] == [] and got["next"] == []


def test_another_users_item_cannot_be_acted_on(world):
    alice = world.api.as_user("alice")
    theirs = _seed_everything("bob", "bob")

    for key in (f"outcome:{theirs['active']}", f"run:{theirs['run']}", "event:ev-bob", "note:note-bob"):
        for action in ("defer", "done", "start", "resume"):
            res = world.act(alice, key=key, action=action)
            assert res.status_code == 404, (key, action, res.text)
    assert world.api.as_user("bob").get(f"/api/outcomes/{theirs['active']}").json()["outcome"]["state"] == "active"
