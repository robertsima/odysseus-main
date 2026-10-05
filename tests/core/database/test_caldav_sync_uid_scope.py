"""CalDAV sync must not hijack another user's event via a shared VEVENT uid.

CalendarEvent.uid is the global primary key. _sync_blocking looked up the
existing event by uid with NO calendar scope, so when user B synced a uid
that user A's calendar already held, the query returned A's row and the sync
reassigned its calendar_id to B's calendar — stealing A's event. The lookup
must be scoped to the calendar being synced.
"""
import uuid
from datetime import datetime

import pytest

from core.database import CalendarEvent, CalendarCal
from src.caldav_sync import _find_existing_event

pytestmark = pytest.mark.security


def _setup(app_db):
    db = app_db.SessionLocal()
    try:
        db.add(CalendarCal(id="calA", owner="alice", name="A"))
        db.add(CalendarCal(id="calB", owner="bob", name="B"))
        # dtstart/dtend are NOT NULL in the schema, so seed valid values.
        db.add(CalendarEvent(
            uid="shared@svc", calendar_id="calA", summary="Alice event",
            dtstart=datetime(2026, 6, 4, 9, 0), dtend=datetime(2026, 6, 4, 10, 0),
        ))
        db.commit()
    finally:
        db.close()


def test_lookup_for_other_calendar_does_not_find_a_users_event(app_db):
    _setup(app_db)
    db = app_db.SessionLocal()
    try:
        # Bob's calendar syncing the same uid must NOT resolve Alice's row.
        assert _find_existing_event(db, {}, "shared@svc", "calB") is None
        # Same calendar still resolves its own event (normal update path).
        own = _find_existing_event(db, {}, "shared@svc", "calA")
        assert own is not None and own.calendar_id == "calA"
    finally:
        db.close()


def test_alice_event_is_not_moved(app_db):
    _setup(app_db)
    db = app_db.SessionLocal()
    try:
        # Simulate the (fixed) sync deciding there is no existing row for calB.
        assert _find_existing_event(db, {}, "shared@svc", "calB") is None
        ev = db.query(CalendarEvent).filter(CalendarEvent.uid == "shared@svc").first()
        assert ev.calendar_id == "calA"  # unchanged — not hijacked
    finally:
        db.close()


def test_pending_takes_precedence(app_db):
    _setup(app_db)
    db = app_db.SessionLocal()
    try:
        sentinel = object()
        assert _find_existing_event(db, {"shared@svc": sentinel}, "shared@svc", "calB") is sentinel
    finally:
        db.close()
