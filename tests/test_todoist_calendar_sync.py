import asyncio
import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import CalendarCal, CalendarEvent
from src.todoist_calendar_sync import (
    _todoist_calendar_id,
    _todoist_event_uid,
    sync_todoist_calendar,
)


@pytest.fixture()
def todoist_db(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'todoist-calendar.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(cdb, "SessionLocal", Session)
    return Session


def _task(task_id, content, due_date):
    return {
        "id": task_id,
        "content": content,
        "description": "from todoist",
        "priority": 4,
        "due": {"date": due_date},
        "labels": ["work"],
        "url": f"https://todoist.example/{task_id}",
    }


def test_sync_todoist_calendar_upserts_dated_tasks(monkeypatch, todoist_db):
    monkeypatch.setenv("TODOIST_API_TOKEN", "token")
    tasks = [_task("abc", "Write proposal", "2026-08-10")]

    async def fake_run_td(args, timeout=60):
        assert args == ["task", "list", "--json"]
        return 0, json.dumps({"results": tasks}), ""

    monkeypatch.setattr("src.todoist_calendar_sync._run_td", fake_run_td)

    result = asyncio.run(sync_todoist_calendar("alice@example.com"))

    assert result["calendars"] == 1
    assert result["events"] == 1
    db = todoist_db()
    try:
        cal = db.query(CalendarCal).filter_by(id=_todoist_calendar_id("alice@example.com")).one()
        ev = db.query(CalendarEvent).filter_by(uid=_todoist_event_uid("abc")).one()
        assert cal.name == "Todoist"
        assert cal.source == "todoist"
        assert ev.calendar_id == cal.id
        assert ev.summary == "Write proposal"
        assert ev.all_day is True
        assert ev.dtstart == datetime(2026, 8, 10)
        assert ev.importance == "critical"
        assert ev.origin == "todoist"
        assert "https://todoist.example/abc" in ev.description
    finally:
        db.close()


def test_sync_todoist_calendar_prunes_removed_todoist_events(monkeypatch, todoist_db):
    monkeypatch.setenv("TODOIST_API_TOKEN", "token")
    owner = "alice@example.com"
    cal_id = _todoist_calendar_id(owner)
    db = todoist_db()
    try:
        db.add(CalendarCal(id=cal_id, owner=owner, name="Todoist", source="todoist"))
        db.add(CalendarEvent(
            uid=_todoist_event_uid("gone"),
            calendar_id=cal_id,
            summary="Gone",
            dtstart=datetime(2026, 8, 10),
            dtend=datetime(2026, 8, 11),
            all_day=True,
            origin="todoist",
        ))
        db.add(CalendarEvent(
            uid="local-event",
            calendar_id=cal_id,
            summary="Local",
            dtstart=datetime(2026, 8, 10),
            dtend=datetime(2026, 8, 11),
            all_day=True,
            origin=None,
        ))
        db.commit()
    finally:
        db.close()

    async def fake_run_td(args, timeout=60):
        return 0, json.dumps({"results": [_task("kept", "Kept", "2026-08-12")]}), ""

    monkeypatch.setattr("src.todoist_calendar_sync._run_td", fake_run_td)

    result = asyncio.run(sync_todoist_calendar(owner))

    assert result["deleted"] == 1
    db = todoist_db()
    try:
        assert db.query(CalendarEvent).filter_by(uid=_todoist_event_uid("gone")).first() is None
        assert db.query(CalendarEvent).filter_by(uid="local-event").first() is not None
        assert db.query(CalendarEvent).filter_by(uid=_todoist_event_uid("kept")).first() is not None
    finally:
        db.close()


def test_sync_todoist_calendar_skips_without_token(monkeypatch):
    monkeypatch.delenv("TODOIST_API_TOKEN", raising=False)

    result = asyncio.run(sync_todoist_calendar("alice@example.com"))

    assert result["skipped"] is True
    assert result["errors"] == []
