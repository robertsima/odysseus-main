"""The agent's manage_calendar writes go through the same CalDAV write-through as the UI.

A model that creates, edits or deletes an event on a CalDAV calendar must get
the event pushed to the server and, for a delete, a tombstone so a failed
remote delete is retried. If the tool wrote only the local row, the event the
assistant "created" would never reach the user's phone and the next pull could
prune it. Only the network push is faked.
"""
import json
from datetime import datetime

import pytest

import src.database
from core.database import CalendarCal, CalendarDeletedEvent, CalendarEvent
from src import caldav_sync
from src.tools.calendar import do_manage_calendar

REMOTE = "https://caldav.test/calendars/alice/main/"


@pytest.fixture
def pushes(api, monkeypatch):
    calls = []

    def recorder(action):
        async def push(owner, uid):
            calls.append((action, owner, uid))
            return {"ok": True}
        return push

    monkeypatch.setattr(caldav_sync, "push_event_create", recorder("create"))
    monkeypatch.setattr(caldav_sync, "push_event_update", recorder("update"))
    monkeypatch.setattr(caldav_sync, "push_event_delete", recorder("delete"))

    db = src.database.SessionLocal()
    try:
        db.add(CalendarCal(id="cal-remote", owner="alice", name="Remote", source="caldav", caldav_base_url=REMOTE))
        db.add(CalendarEvent(
            uid="remote-event", calendar_id="cal-remote", summary="Synced", origin="caldav",
            dtstart=datetime(2026, 6, 2, 10), dtend=datetime(2026, 6, 2, 11),
            remote_href=f"{REMOTE}remote-event.ics",
        ))
        db.commit()
    finally:
        db.close()
    return calls


async def _tool(**args):
    result = await do_manage_calendar(json.dumps(args), owner="alice")
    assert result["exit_code"] == 0, result
    return result


@pytest.mark.asyncio
async def test_a_created_event_on_a_caldav_calendar_is_pushed(pushes):
    result = await _tool(action="create_event", summary="Dentist", dtstart="2026-06-03T09:00:00",
                         calendar_href="cal-remote")

    assert pushes == [("create", "alice", result["uid"])]


@pytest.mark.asyncio
async def test_an_updated_event_is_pushed_and_marked_pending(pushes):
    await _tool(action="update_event", uid="remote-event", summary="Renamed")

    assert pushes == [("update", "alice", "remote-event")]
    db = src.database.SessionLocal()
    try:
        assert db.query(CalendarEvent).filter_by(uid="remote-event").one().caldav_sync_pending == "update"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_deleted_event_is_pushed_and_leaves_a_tombstone(pushes):
    await _tool(action="delete_event", uid="remote-event")

    assert pushes == [("delete", "alice", "remote-event")]
    db = src.database.SessionLocal()
    try:
        assert db.query(CalendarEvent).filter_by(uid="remote-event").first() is None
        tombstone = db.query(CalendarDeletedEvent).filter_by(uid="remote-event").one()
        assert tombstone.remote_href == f"{REMOTE}remote-event.ics"
    finally:
        db.close()
