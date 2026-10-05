"""A CalDAV pull must not discard what was edited here and not yet pushed.

The pull overwrites local rows with the server's copy and prunes rows the
server no longer returns. A row with a pending create or update, or one that
was never pushed (no remote_href), holds the user's work: overwriting or
pruning it loses an event the server has never seen. The pull runs against a
fake CalDAV client; the database is real.
"""
from datetime import datetime, timedelta

import pytest

import core.database
from core.database import CalendarCal, CalendarEvent
from src import caldav_sync

URL = "https://caldav.test/calendars/alice/main/"


def _ics(uid, summary, start):
    stamp = start.strftime("%Y%m%dT%H%M%SZ")
    end = (start + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ")
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nSUMMARY:{summary}\r\nDTSTART:{stamp}\r\nDTEND:{end}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


class _Resource:
    def __init__(self, uid, summary, start):
        self.data = _ics(uid, summary, start)
        self.url = f"{URL}{uid}.ics"
        self.etag = '"1"'


class _Calendar:
    name = "Main"
    url = URL

    def __init__(self, resources):
        self.resources = resources

    def date_search(self, start=None, end=None, expand=False):
        return self.resources


class _Client:
    def __init__(self, resources):
        calendar = _Calendar(resources)
        self.principal = lambda: type("Principal", (), {"calendars": lambda self: [calendar]})()

    def close(self):
        pass


def _row(uid, summary, start, **fields):
    return CalendarEvent(
        uid=uid, calendar_id=CAL_ID, summary=summary, dtstart=start,
        dtend=start + timedelta(hours=1), origin="caldav", **fields,
    )


CAL_ID = caldav_sync._stable_cal_id(URL, owner="alice", account_id="")


@pytest.fixture
def pull(app_db, monkeypatch):
    """Seed alice's CalDAV calendar, run one pull, return the events by uid."""
    monkeypatch.setattr(core.database, "SessionLocal", app_db.SessionLocal)
    soon = datetime.utcnow().replace(microsecond=0) + timedelta(days=2)

    def run(local_rows, server_resources):
        db = app_db.SessionLocal()
        try:
            db.add(CalendarCal(id=CAL_ID, owner="alice", name="Main", source="caldav", caldav_base_url=URL))
            for row in local_rows:
                db.add(row)
            db.commit()
        finally:
            db.close()
        monkeypatch.setattr(caldav_sync, "_build_dav_client", lambda *a, **k: _Client(server_resources))
        result = caldav_sync._sync_blocking("alice", URL, "alice", "secret")
        assert not result["errors"], result["errors"]
        db = app_db.SessionLocal()
        try:
            return {e.uid: e.summary for e in db.query(CalendarEvent)}
        finally:
            db.close()

    run.soon = soon
    return run


def test_pending_and_never_pushed_rows_survive_a_pull_that_does_not_return_them(pull):
    soon = pull.soon
    events = pull(
        [
            _row("pending-edit", "Edited here", soon, remote_href=f"{URL}pending-edit.ics",
                 caldav_sync_pending="update"),
            _row("never-pushed", "Created here", soon),
            _row("deleted-upstream", "Gone on the server", soon, remote_href=f"{URL}deleted-upstream.ics"),
        ],
        [_Resource("still-there", "Still there", soon)],
    )

    assert events["pending-edit"] == "Edited here"
    assert events["never-pushed"] == "Created here"
    assert "deleted-upstream" not in events
    assert events["still-there"] == "Still there"


def test_the_servers_copy_does_not_overwrite_an_unpushed_edit(pull):
    soon = pull.soon
    events = pull(
        [_row("pending-edit", "Edited here", soon, remote_href=f"{URL}pending-edit.ics",
              caldav_sync_pending="update")],
        [_Resource("pending-edit", "Older server title", soon)],
    )

    assert events["pending-edit"] == "Edited here"
