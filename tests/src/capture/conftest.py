"""Fixtures for src.capture: a throwaway database, an in-memory notes store, a fake `td`,
a fake utility model and a pinned clock. The capture code itself stays real."""
import asyncio
import json
import threading
from datetime import datetime

import pytest

NOW_UTC = datetime(2026, 10, 10, 14, 0, 0)  # a Saturday
ADMINS = {"admin"}


class FakeTd:
    """Stands in for `td` behind `_run_td`: keeps a task list and records every call."""

    def __init__(self):
        self.tasks = []
        self.calls = []
        self.creates = 0
        self.deletes = []
        self.fail_list = False
        self.timeout_after_create = False
        self.fail_create = None
        self.create_delay = 0.0
        self._lock = threading.Lock()
        self._next = 1

    def seed(self, content, due=None):
        with self._lock:
            task = {"id": f"seed{self._next}", "content": content,
                    "due": {"date": due} if due else None, "labels": []}
            self._next += 1
            self.tasks.append(task)
        return task

    async def __call__(self, args, timeout=60):
        await asyncio.sleep(0)
        self.calls.append(list(args))
        if args[:2] == ["task", "list"]:
            if self.fail_list:
                return 1, "", "Todoist CLI timed out after 15 seconds"
            return 0, json.dumps({"results": list(self.tasks)}), ""
        if args[:2] == ["task", "add"]:
            if self.fail_create:
                return 1, "", self.fail_create
            if self.create_delay:
                await asyncio.sleep(self.create_delay)
            with self._lock:
                self.creates += 1
                task = {"id": f"td{self._next}", "content": args[2], "labels": [],
                        "due": {"date": args[args.index("--due") + 1]} if "--due" in args else None}
                self._next += 1
                self.tasks.append(task)
            if self.timeout_after_create:
                return 1, "", "Todoist CLI timed out after 60 seconds"
            return 0, json.dumps(task), ""
        if args[:2] == ["task", "delete"]:
            self.deletes.append(args[2])
            self.tasks = [t for t in self.tasks if t["id"] != args[2]]
            return 0, "", ""
        return 1, "", f"unexpected td call {args}"

    def added(self):
        return [c for c in self.calls if c[:2] == ["task", "add"]]


class FakeModel:
    """The utility model: replies are queued; an unexpected call fails the test."""

    def __init__(self):
        self.replies = []
        self.prompts = []

    async def __call__(self, **kwargs):
        self.prompts.append(kwargs["messages"])
        if not self.replies:
            raise AssertionError("the utility model was called but no reply was queued")
        return self.replies.pop(0)


class Clock:
    def __init__(self):
        self.now = NOW_UTC

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    from src import capture

    c = Clock()
    monkeypatch.setattr(capture, "_utcnow", c)
    return c


@pytest.fixture
def td(monkeypatch):
    from src import capture

    fake = FakeTd()
    monkeypatch.setenv("TODOIST_API_TOKEN", "test-token")
    monkeypatch.setattr(capture, "_run_td", fake)
    return fake


@pytest.fixture
def model(monkeypatch):
    import src.endpoint_resolver as resolver
    import src.llm_core as llm_core

    fake = FakeModel()
    monkeypatch.setattr(resolver, "resolve_endpoint", lambda *a, **k: ("http://model.test", "m", {}))
    monkeypatch.setattr(llm_core, "llm_call_async", fake)
    return fake


@pytest.fixture
def notes(monkeypatch):
    from src import notes_store
    from tests.helpers.fake_notes_store import FakeNotesStore

    store = FakeNotesStore()
    monkeypatch.setattr(notes_store, "STORE", store)
    return store


@pytest.fixture
def env(monkeypatch, app_db, clock, td, model, notes):
    """Everything a capture needs. `admin` may use Todoist; every other owner may not."""
    import core.database as cdb
    import routes.calendar_routes as cr
    import src.tool_security as tool_security
    from src import outcomes

    monkeypatch.setattr(cdb, "SessionLocal", app_db.SessionLocal)
    monkeypatch.setattr(cr, "SessionLocal", app_db.SessionLocal, raising=False)
    monkeypatch.setattr(tool_security, "owner_is_admin_or_single_user", lambda owner: owner in ADMINS)

    class Env:
        pass

    e = Env()
    e.db, e.td, e.model, e.notes, e.clock, e.outcomes = app_db, td, model, notes, clock, outcomes
    return e


@pytest.fixture
def events(env):
    """Calendar events written for an owner, read back from the real table."""
    from core.database import CalendarCal, CalendarEvent

    def read(owner):
        db = env.db.SessionLocal()
        try:
            rows = (db.query(CalendarEvent).join(CalendarCal, CalendarEvent.calendar_id == CalendarCal.id)
                    .filter(CalendarCal.owner == owner).all())
            return [(r.summary, r.dtstart, r.all_day) for r in rows]
        finally:
            db.close()
    return read


@pytest.fixture
def ledger(env):
    from core.database import CaptureRequest

    def rows(owner=None):
        db = env.db.SessionLocal()
        try:
            q = db.query(CaptureRequest)
            if owner:
                q = q.filter(CaptureRequest.owner == owner)
            return [(r.owner, r.key, r.status) for r in q.all()]
        finally:
            db.close()
    return rows


@pytest.fixture
def cap(env):
    """`capture(...)` with UTC as the person's timezone so dates are the same on every machine."""
    from src import capture

    async def call(owner, text, **kw):
        kw.setdefault("tz_offset", 0)
        return await capture.capture(owner, text, **kw)
    return call
