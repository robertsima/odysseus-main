"""Calendar writes on a CalDAV calendar are marked pending and pushed after the commit.

The local write is authoritative: the row is stored first, flagged so /sync can
retry, and only then pushed. Without the flag a failed push leaves an event
that exists only here and that the next pull may overwrite or prune; without
the delete tombstone a failed remote delete is never retried and the event
comes back on the next pull. Only the network push is faked.
"""
from datetime import datetime

import pytest

import src.database
from core.database import CalendarCal, CalendarDeletedEvent, CalendarEvent
from src import caldav_sync

REMOTE = "https://caldav.test/calendars/alice/main/"


@pytest.fixture
def pushes(monkeypatch):
    """Record the pushes the routes make; ``fail`` makes the next ones fail."""
    calls = []

    class Pushes:
        failing = False

        def record(self, action):
            async def push(owner, uid):
                calls.append((action, owner, uid))
                return {"ok": False, "error": "server down"} if self.failing else {"ok": True}
            return push

    fake = Pushes()
    fake.calls = calls
    monkeypatch.setattr(caldav_sync, "push_event_create", fake.record("create"))
    monkeypatch.setattr(caldav_sync, "push_event_update", fake.record("update"))
    monkeypatch.setattr(caldav_sync, "push_event_delete", fake.record("delete"))
    return fake


@pytest.fixture
def calendars(api):
    db = src.database.SessionLocal()
    try:
        db.add(CalendarCal(id="cal-remote", owner="alice", name="Remote", source="caldav", caldav_base_url=REMOTE))
        db.add(CalendarCal(id="cal-local", owner="alice", name="Local"))
        db.add(CalendarEvent(
            uid="remote-event", calendar_id="cal-remote", summary="Synced", origin="caldav",
            dtstart=datetime(2026, 6, 2, 10), dtend=datetime(2026, 6, 2, 11),
            remote_href=f"{REMOTE}remote-event.ics",
        ))
        db.add(CalendarEvent(
            uid="local-event", calendar_id="cal-local", summary="Private",
            dtstart=datetime(2026, 6, 2, 10), dtend=datetime(2026, 6, 2, 11),
        ))
        db.commit()
    finally:
        db.close()


def _event(uid):
    db = src.database.SessionLocal()
    try:
        ev = db.query(CalendarEvent).filter_by(uid=uid).first()
        return ev and {"summary": ev.summary, "pending": ev.caldav_sync_pending}
    finally:
        db.close()


def _tombstones():
    db = src.database.SessionLocal()
    try:
        return {t.uid: t.remote_href for t in db.query(CalendarDeletedEvent)}
    finally:
        db.close()


def _create(client, calendar_id):
    response = client.post("/api/calendar/events", json={
        "summary": "Planning", "dtstart": "2026-06-03T09:00:00", "calendar_href": calendar_id,
    })
    assert response.status_code == 200, response.text
    return response.json()["uid"]


def test_a_new_event_on_a_caldav_calendar_is_pushed(api, calendars, pushes):
    uid = _create(api.as_user("alice"), "cal-remote")

    assert pushes.calls == [("create", "alice", uid)]


def test_a_failed_push_leaves_the_new_event_pending_for_the_next_sync(api, calendars, pushes):
    pushes.failing = True

    uid = _create(api.as_user("alice"), "cal-remote")

    assert _event(uid) == {"summary": "Planning", "pending": "create"}


def test_a_new_event_on_a_local_calendar_is_never_pushed(api, calendars, pushes):
    uid = _create(api.as_user("alice"), "cal-local")

    assert pushes.calls == []
    assert _event(uid)["pending"] is None


def test_an_update_is_pushed_and_a_failed_push_leaves_it_pending(api, calendars, pushes):
    client = api.as_user("alice")
    assert client.put("/api/calendar/events/remote-event", json={"summary": "Renamed"}).status_code == 200
    assert pushes.calls == [("update", "alice", "remote-event")]

    pushes.failing = True
    assert client.put("/api/calendar/events/remote-event", json={"summary": "Renamed again"}).status_code == 200
    assert _event("remote-event") == {"summary": "Renamed again", "pending": "update"}


def test_deleting_a_synced_event_pushes_the_delete_and_keeps_a_tombstone(api, calendars, pushes):
    response = api.as_user("alice").delete("/api/calendar/events/remote-event")

    assert response.status_code == 200
    assert _event("remote-event") is None
    assert pushes.calls == [("delete", "alice", "remote-event")]
    assert _tombstones() == {"remote-event": f"{REMOTE}remote-event.ics"}


def test_deleting_a_local_event_leaves_no_tombstone_and_no_push(api, calendars, pushes):
    response = api.as_user("alice").delete("/api/calendar/events/local-event")

    assert response.status_code == 200
    assert pushes.calls == []
    assert _tombstones() == {}
