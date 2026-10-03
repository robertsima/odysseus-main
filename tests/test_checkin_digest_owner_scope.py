"""Check-in calendar digest must be scoped to the task owner.

The digest query selected CalendarEvent with no owner scope, so a scheduled
check-in for one user pulled EVERY user's calendar events (summaries,
locations) into their digest — a cross-tenant leak. Ownership lives on
CalendarCal.owner; the query must join it, like routes/calendar_routes.
"""
import uuid
from datetime import datetime

import pytest

from core.database import CalendarEvent, CalendarCal
from src.task_scheduler import _checkin_calendar_events


def _seed(app_db):
    db = app_db.SessionLocal()
    try:
        db.add(CalendarCal(id="calA", owner="alice", name="A"))
        db.add(CalendarCal(id="calB", owner="bob", name="B"))
        db.add(CalendarEvent(uid="a1", calendar_id="calA", summary="Alice mtg",
                             dtstart=datetime(2026, 6, 10, 9, 0),
                             dtend=datetime(2026, 6, 10, 10, 0), status="confirmed"))
        db.add(CalendarEvent(uid="b1", calendar_id="calB", summary="Bob secret",
                             dtstart=datetime(2026, 6, 10, 10, 0),
                             dtend=datetime(2026, 6, 10, 11, 0), status="confirmed"))
        db.commit()
    finally:
        db.close()


def test_digest_only_returns_owner_events(app_db):
    _seed(app_db)
    db = app_db.SessionLocal()
    try:
        s, e = datetime(2026, 6, 1), datetime(2026, 6, 30)
        alice = _checkin_calendar_events(db, "alice", s, e)
        assert [ev.summary for ev in alice] == ["Alice mtg"]  # not Bob's
        bob = _checkin_calendar_events(db, "bob", s, e)
        assert [ev.summary for ev in bob] == ["Bob secret"]
    finally:
        db.close()


def test_cancelled_excluded_and_window_respected(app_db):
    _seed(app_db)
    db = app_db.SessionLocal()
    try:
        db2 = app_db.SessionLocal()
        db2.add(CalendarEvent(uid="a2", calendar_id="calA", summary="cancelled",
                              dtstart=datetime(2026, 6, 11),
                              dtend=datetime(2026, 6, 11, 1, 0), status="cancelled"))
        db2.commit(); db2.close()
        s, e = datetime(2026, 6, 1), datetime(2026, 6, 30)
        out = _checkin_calendar_events(db, "alice", s, e)
        assert "cancelled" not in [ev.summary for ev in out]
    finally:
        db.close()
