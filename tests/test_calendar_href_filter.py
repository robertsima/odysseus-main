"""list_events must honour the calendar identifier it hands out itself.

The schema advertises `calendar_href`, every event list_events returns carries
a `calendar_href`, and create_event accepts it — but list_events read only
`calendar`. A model that echoed back the href it had just been given was
therefore answered from EVERY calendar the owner has, including the one
Todoist tasks are imported into. That is why asking for one specific calendar
kept coming back full of Todoist items.
"""

import json
import sys
import tempfile
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

import core.database as cdb

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)


@pytest.fixture(autouse=True)
def _bind_temp_db(monkeypatch):
    monkeypatch.setitem(sys.modules, "core.database", cdb)
    parent = sys.modules.get("core")
    if parent is not None:
        monkeypatch.setattr(parent, "database", cdb, raising=False)
    monkeypatch.setattr(cdb, "SessionLocal", _TS)
    yield


RANGE = {"start": "2126-03-01T00:00:00Z", "end": "2126-04-01T00:00:00Z"}


async def _two_calendars(owner):
    """A 'Todoist' calendar and a real one, each with one event."""
    from src.tool_implementations import do_manage_calendar

    db = _TS()
    try:
        todoist = cdb.CalendarCal(
            id=uuid.uuid4().hex, owner=owner, name="Todoist", source="local", color="#fff",
        )
        work = cdb.CalendarCal(
            id=uuid.uuid4().hex, owner=owner, name="Work", source="caldav", color="#000",
        )
        db.add_all([todoist, work])
        db.commit()
        ids = (todoist.id, work.id)
    finally:
        db.close()

    for cal_id, summary in zip(ids, ["Buy milk", "Design review"]):
        res = await do_manage_calendar(json.dumps({
            "action": "create_event",
            "summary": summary,
            "dtstart": "2126-03-10T10:00:00Z",
            "calendar_href": cal_id,
        }), owner=owner)
        assert res.get("exit_code", 0) == 0, res
    return ids


async def test_calendar_href_scopes_the_listing():
    from src.tool_implementations import do_manage_calendar

    owner = "cal-href-" + uuid.uuid4().hex[:8]
    todoist_id, work_id = await _two_calendars(owner)

    res = await do_manage_calendar(json.dumps({
        "action": "list_events", "calendar_href": work_id, **RANGE,
    }), owner=owner)

    assert res.get("exit_code", 0) == 0, res
    assert [e["summary"] for e in res["events"]] == ["Design review"]
    assert all(e["calendar_href"] == work_id for e in res["events"])


async def test_the_href_an_event_carries_can_be_fed_straight_back():
    """The round trip the model actually performs."""
    from src.tool_implementations import do_manage_calendar

    owner = "cal-roundtrip-" + uuid.uuid4().hex[:8]
    await _two_calendars(owner)

    everything = await do_manage_calendar(json.dumps({
        "action": "list_events", **RANGE,
    }), owner=owner)
    assert len(everything["events"]) == 2

    href = next(e["calendar_href"] for e in everything["events"] if e["summary"] == "Buy milk")
    scoped = await do_manage_calendar(json.dumps({
        "action": "list_events", "calendar_href": href, **RANGE,
    }), owner=owner)
    assert [e["summary"] for e in scoped["events"]] == ["Buy milk"]


async def test_calendar_alias_and_name_still_work():
    from src.tool_implementations import do_manage_calendar

    owner = "cal-alias-" + uuid.uuid4().hex[:8]
    await _two_calendars(owner)

    by_name = await do_manage_calendar(json.dumps({
        "action": "list_events", "calendar": "Work", **RANGE,
    }), owner=owner)
    assert [e["summary"] for e in by_name["events"]] == ["Design review"]


async def test_short_id_prefix_from_list_calendars_resolves():
    """list_calendars prints an 8-char prefix, so that is what a model sends."""
    from src.tool_implementations import do_manage_calendar

    owner = "cal-prefix-" + uuid.uuid4().hex[:8]
    _todoist_id, work_id = await _two_calendars(owner)

    res = await do_manage_calendar(json.dumps({
        "action": "list_events", "calendar_href": work_id[:8], **RANGE,
    }), owner=owner)
    assert [e["summary"] for e in res["events"]] == ["Design review"]


async def test_an_unknown_calendar_says_so_instead_of_listing_everything():
    """Silently widening to all calendars is the bug; an error is the fix."""
    from src.tool_implementations import do_manage_calendar

    owner = "cal-unknown-" + uuid.uuid4().hex[:8]
    await _two_calendars(owner)

    res = await do_manage_calendar(json.dumps({
        "action": "list_events", "calendar_href": "no-such-calendar", **RANGE,
    }), owner=owner)
    assert res.get("exit_code") == 1, res
    assert "list_calendars" in res.get("error", "")


async def test_list_calendars_reports_an_id_alongside_the_legacy_href():
    from src.tool_implementations import do_manage_calendar

    owner = "cal-ids-" + uuid.uuid4().hex[:8]
    await _two_calendars(owner)

    res = await do_manage_calendar(json.dumps({"action": "list_calendars"}), owner=owner)
    assert res.get("exit_code", 0) == 0, res
    assert all(c["id"] == c["href"] for c in res["calendars"])
