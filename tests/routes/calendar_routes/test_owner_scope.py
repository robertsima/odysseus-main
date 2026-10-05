"""Calendars and their events belong to one user.

Alice and bob each own a calendar with one event; a third, legacy calendar
has no owner. Nobody may add events to, list, or export a calendar that is
not theirs, and a legacy ownerless calendar is nobody's.
"""
from datetime import datetime

import pytest

import src.database
from core.database import CalendarCal, CalendarEvent

pytestmark = pytest.mark.security

RANGE = {"start": "2026-06-01T00:00:00", "end": "2026-06-30T00:00:00"}


@pytest.fixture
def calendars(api):
    db = src.database.SessionLocal()
    try:
        for cal_id, owner in (("cal-alice", "alice"), ("cal-bob", "bob"), ("cal-legacy", None)):
            db.add(CalendarCal(id=cal_id, owner=owner, name=f"{cal_id} calendar"))
            db.add(CalendarEvent(uid=f"{cal_id}-event", calendar_id=cal_id, summary=f"{cal_id} meeting",
                                 dtstart=datetime(2026, 6, 2, 10), dtend=datetime(2026, 6, 2, 11)))
        db.commit()
    finally:
        db.close()


def _events_in(cal_id):
    db = src.database.SessionLocal()
    try:
        return {e.uid for e in db.query(CalendarEvent).filter(CalendarEvent.calendar_id == cal_id)}
    finally:
        db.close()


@pytest.mark.parametrize("cal_id", ["cal-bob", "cal-legacy"])
def test_an_event_cannot_be_added_to_a_calendar_you_do_not_own(api, calendars, cal_id):
    response = api.as_user("alice").post("/api/calendar/events", json={
        "summary": "planted", "dtstart": "2026-06-03T09:00:00", "calendar_href": cal_id,
    })

    assert response.status_code == 404
    assert _events_in(cal_id) == {f"{cal_id}-event"}


def test_listing_events_shows_only_your_calendars(api, calendars):
    response = api.as_user("alice").get("/api/calendar/events", params=RANGE)

    assert response.status_code == 200
    assert [event["uid"] for event in response.json()["events"]] == ["cal-alice-event"]


@pytest.mark.parametrize("cal_id", ["cal-bob", "cal-legacy"])
def test_a_calendar_you_do_not_own_cannot_be_exported(api, calendars, cal_id):
    response = api.as_user("alice").get(f"/api/calendar/export/{cal_id}")

    assert response.status_code == 404
    assert "meeting" not in response.text


def test_an_export_file_name_cannot_inject_headers(api):
    db = src.database.SessionLocal()
    try:
        db.add(CalendarCal(id="cal-odd", owner="alice", name='Work"\r\nX-Injected: yes; ../evil'))
        db.commit()
    finally:
        db.close()

    response = api.as_user("alice").get("/api/calendar/export/cal-odd")

    assert response.status_code == 200
    assert "x-injected" not in {name.lower() for name in response.headers}
    disposition = response.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert "/" not in disposition.split("filename=", 1)[1]
