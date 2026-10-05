"""get_upcoming_events(owner) returns only that owner's events.

The email-to-calendar pass runs over every user's mailbox and shows this
snapshot to the extraction model. Unscoped, processing alice's mail would
disclose bob's events to the model and let it act on them.
"""
from datetime import timedelta

import pytest

import core.database
from core.database import CalendarCal, CalendarEvent, get_upcoming_events, utcnow_naive

pytestmark = pytest.mark.security


def test_upcoming_events_are_the_owners_only(app_db, monkeypatch):
    monkeypatch.setattr(core.database, "SessionLocal", app_db.SessionLocal)
    soon = utcnow_naive() + timedelta(days=1)
    db = app_db.SessionLocal()
    for owner in ("alice", "bob"):
        db.add(CalendarCal(id=f"cal-{owner}", owner=owner, name=owner))
        db.add(CalendarEvent(uid=f"{owner}-event", calendar_id=f"cal-{owner}", summary=f"{owner} dentist",
                             dtstart=soon, dtend=soon + timedelta(hours=1)))
    db.commit()
    db.close()

    events = get_upcoming_events("alice")

    assert [event["uid"] for event in events] == ["alice-event"]
