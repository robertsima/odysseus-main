"""A stale calendar id must not wedge the sync forever.

_stable_cal_id hashes owner+account_id+url, so any change to that derivation
gives a remote calendar a new local id. Every event already stored under the
old id then fails the calendar-scoped lookup, and because CalendarEvent.uid is
the GLOBAL primary key the sync re-inserts all of them and the batch commit
dies on UNIQUE constraint failed: calendar_events.uid — discarding every event
parsed for that calendar, every cycle, permanently.

Same-owner rows are adopted instead. Another owner's row is still never taken
(see test_caldav_sync_uid_scope.py); it just gets skipped rather than killing
the batch.
"""
import tempfile
from datetime import datetime

from sqlalchemy import create_engine, text as sa_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import CalendarEvent, CalendarCal
from src.caldav_sync import _find_existing_event

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)


def _seed(*, stale_owner="alice", with_stale_cal=True):
    db = _TS()
    try:
        db.query(CalendarEvent).delete()
        db.query(CalendarCal).delete()
        if with_stale_cal:
            db.add(CalendarCal(id="caldav-OLD", owner=stale_owner, name="old"))
        db.add(CalendarCal(id="caldav-NEW", owner="alice", name="new"))
        db.add(CalendarEvent(
            uid="evt@google.com", calendar_id="caldav-OLD", summary="Interview",
            dtstart=datetime(2026, 8, 26, 17, 30), dtend=datetime(2026, 8, 26, 18, 0),
        ))
        db.commit()
    finally:
        db.close()


def test_same_owner_stale_calendar_id_is_adopted():
    _seed()
    db = _TS()
    try:
        found = _find_existing_event(db, {}, "evt@google.com", "caldav-NEW", "alice")
        assert found is not None, "event stranded under the old calendar id was not adopted"
        assert found.calendar_id == "caldav-OLD"  # caller re-points it
    finally:
        db.close()


def test_orphaned_row_whose_calendar_vanished_is_reclaimed():
    """PRAGMA foreign_keys=ON (core/database.py:146) plus the delete-orphan
    cascade means the app can no longer create an event whose calendar is gone.
    Rows predating that enforcement can still exist, so seed one the only way
    it could have arisen — with the pragma off — and check we reclaim it rather
    than skipping it forever. Nobody owns it, so adopting cannot steal it.
    """
    _seed()
    db = _TS()
    try:
        db.execute(sa_text("PRAGMA foreign_keys=OFF"))
        db.execute(sa_text("DELETE FROM calendars WHERE id='caldav-OLD'"))
        db.commit()
        assert _find_existing_event(db, {}, "evt@google.com", "caldav-NEW", "alice") is not None
    finally:
        db.execute(sa_text("PRAGMA foreign_keys=ON"))
        db.close()


def test_other_owners_row_is_still_never_adopted():
    _seed(stale_owner="bob")
    db = _TS()
    try:
        assert _find_existing_event(db, {}, "evt@google.com", "caldav-NEW", "alice") is None
    finally:
        db.close()


def test_unknown_owner_is_not_adopted():
    """owner="" must stay conservative — adopting on a missing owner would
    reintroduce the cross-user hijack."""
    _seed(stale_owner="bob")
    db = _TS()
    try:
        assert _find_existing_event(db, {}, "evt@google.com", "caldav-NEW", "") is None
    finally:
        db.close()


def test_savepoint_isolates_a_colliding_insert_from_the_batch():
    """The behaviour the sync relies on: one doomed insert must not take the
    other events in the batch down with it."""
    _seed(stale_owner="bob")
    db = _TS()
    try:
        good = CalendarEvent(
            uid="fine@google.com", calendar_id="caldav-NEW", summary="Keeps",
            dtstart=datetime(2026, 8, 26, 9, 0), dtend=datetime(2026, 8, 26, 10, 0),
        )
        db.add(good)
        db.flush()

        sp = db.begin_nested()
        try:
            db.add(CalendarEvent(
                uid="evt@google.com",  # already held by bob's calendar
                calendar_id="caldav-NEW", summary="Doomed",
                dtstart=datetime(2026, 8, 26, 17, 30), dtend=datetime(2026, 8, 26, 18, 0),
            ))
            db.flush()
        except IntegrityError:
            sp.rollback()
        else:
            sp.commit()
            raise AssertionError("expected the duplicate uid to violate the PK")

        db.commit()

        assert db.query(CalendarEvent).filter_by(uid="fine@google.com").first() is not None, \
            "the good event was rolled back with the colliding one"
        stolen = db.query(CalendarEvent).filter_by(uid="evt@google.com").first()
        assert stolen.calendar_id == "caldav-OLD", "bob's event was modified"
    finally:
        db.close()
