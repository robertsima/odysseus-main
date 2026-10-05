"""Regression coverage for bidirectional CalDAV sync plumbing.

These tests avoid a live CalDAV server. They pin the local invariants that keep
Odysseus-created CalDAV events from being pruned before they can be pushed.
"""

from datetime import datetime
import importlib.util
from pathlib import Path
import sys

from src.caldav_writeback import build_event_ical


def test_event_to_ical_serializes_core_fields_and_rrule():
    ical = build_event_ical({
        "uid": "evt-123",
        "summary": "Planning",
        "description": "Bring notes",
        "location": "HQ",
        "dtstart": datetime(2026, 6, 5, 9, 0),
        "dtend": datetime(2026, 6, 5, 10, 0),
        "all_day": False,
        "is_utc": False,
        "rrule": "FREQ=WEEKLY;COUNT=2",
    })

    assert "UID:evt-123" in ical
    assert "SUMMARY:Planning" in ical
    assert "DESCRIPTION:Bring notes" in ical
    assert "LOCATION:HQ" in ical
    assert "RRULE:FREQ=WEEKLY;COUNT=2" in ical


def test_failed_remote_delete_leaves_tombstone_and_later_retry_cleans_up(tmp_path, monkeypatch):
    import src.caldav_writeback as writeback

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'calendar.db'}")
    spec = importlib.util.spec_from_file_location("core.database", Path("core/database.py"))
    dbmod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "core.database", dbmod)
    spec.loader.exec_module(dbmod)

    CalendarCal = dbmod.CalendarCal
    CalendarDeletedEvent = dbmod.CalendarDeletedEvent
    CalendarEvent = dbmod.CalendarEvent
    TestingSessionLocal = dbmod.SessionLocal

    session = TestingSessionLocal()
    try:
        cal = CalendarCal(
            id="caldav-test",
            owner="alice",
            name="Remote",
            source="caldav",
            caldav_base_url="https://caldav.example/calendars/alice/main/",
        )
        ev = CalendarEvent(
            uid="evt-delete",
            calendar_id=cal.id,
            summary="Delete me",
            dtstart=datetime(2026, 6, 5, 9, 0),
            dtend=datetime(2026, 6, 5, 10, 0),
            remote_href="https://caldav.example/calendars/alice/main/evt-delete.ics",
        )
        session.add(cal)
        session.add(ev)
        session.commit()

        tombstone = CalendarDeletedEvent(
            uid=ev.uid,
            owner="alice",
            calendar_id=ev.calendar_id,
            remote_href=ev.remote_href,
            remote_etag=ev.remote_etag,
            caldav_base_url=cal.caldav_base_url,
            summary=ev.summary,
        )
        session.add(tombstone)
        session.delete(ev)
        session.commit()

        assert session.query(CalendarEvent).filter_by(uid="evt-delete").first() is None
        tombstone = session.query(CalendarDeletedEvent).filter_by(uid="evt-delete").first()
        assert tombstone is not None
        assert tombstone.remote_href.endswith("evt-delete.ics")
    finally:
        session.close()

    writeback._persist_writeback_result(
        "alice",
        "caldav-test",
        "evt-delete",
        {"ok": False, "error": "temporary remote delete failure"},
        delete=True,
    )

    session = TestingSessionLocal()
    try:
        tombstone = session.query(CalendarDeletedEvent).filter_by(uid="evt-delete").first()
        assert tombstone is not None
        assert "temporary remote delete failure" in tombstone.last_error
    finally:
        session.close()

    writeback._persist_writeback_result(
        "alice",
        "caldav-test",
        "evt-delete",
        {"ok": True},
        delete=True,
    )

    session = TestingSessionLocal()
    try:
        assert session.query(CalendarDeletedEvent).filter_by(uid="evt-delete").first() is None
        assert session.query(CalendarEvent).filter_by(uid="evt-delete").first() is None
    finally:
        session.close()
