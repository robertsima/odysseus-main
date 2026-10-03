"""Issue #800 — the calendar write handlers actually trigger CalDAV write-back.

Route-level: proves POST/DELETE /api/calendar/events fire writeback_event for a
CalDAV-backed calendar and not for a local one.

Calls the async route handlers DIRECTLY (extracted from the router) rather than
through Starlette's TestClient — the TestClient middleware-app + threadpool could
hang in some environments; a direct call with a minimal fake request keeps the
same coverage and completes reliably.
"""

import uuid
from types import SimpleNamespace

import pytest

import routes.calendar_routes as croutes
import src.caldav_sync as csync
from core.database import CalendarCal
from routes.calendar_routes import EventCreate


@pytest.fixture(autouse=True)
def _bind_test_db(monkeypatch, app_db):
    monkeypatch.setattr(croutes, "SessionLocal", app_db.SessionLocal)


@pytest.fixture
def calls(monkeypatch):
    recorded = []

    async def _fake_create(owner, uid):
        recorded.append({"uid": uid, "delete": False, "action": "create"})
        return {"ok": True}

    async def _fake_delete(owner, uid):
        recorded.append({"uid": uid, "delete": True, "action": "delete"})
        return {"ok": True}

    monkeypatch.setattr(csync, "push_event_create", _fake_create)
    monkeypatch.setattr(csync, "push_event_delete", _fake_delete)
    return recorded


def _req():
    return SimpleNamespace(state=SimpleNamespace(current_user="tester"))


def _endpoint(method, suffix):
    router = croutes.setup_calendar_routes()
    for r in router.routes:
        if getattr(r, "path", "").endswith(suffix) and method in getattr(r, "methods", set()):
            return r.endpoint
    raise RuntimeError(f"{method} *{suffix} not found")


def _make_cal(app_db, source):
    cid = ("caldav-" if source == "caldav" else "loc-") + uuid.uuid4().hex[:10]
    db = app_db.SessionLocal()
    try:
        db.add(CalendarCal(id=cid, owner="tester", name="C", source=source))
        db.commit()
        return cid
    finally:
        db.close()


async def test_create_on_caldav_calendar_pushes_to_remote(calls, app_db):
    create_event = _endpoint("POST", "/events")
    cal_id = _make_cal(app_db, "caldav")
    res = await create_event(_req(), EventCreate(
        summary="Dentist", dtstart="2026-06-10T14:00:00Z", calendar_href=cal_id))
    assert res["ok"] is True
    assert len(calls) == 1
    assert calls[0]["delete"] is False


async def test_create_on_local_calendar_does_not_push(calls, app_db):
    create_event = _endpoint("POST", "/events")
    cal_id = _make_cal(app_db, "local")
    res = await create_event(_req(), EventCreate(
        summary="Local", dtstart="2026-06-10T14:00:00Z", calendar_href=cal_id))
    assert res["ok"] is True
    assert calls == []


async def test_delete_on_caldav_calendar_pushes_delete(calls, app_db):
    create_event = _endpoint("POST", "/events")
    delete_event = _endpoint("DELETE", "/events/{uid}")
    cal_id = _make_cal(app_db, "caldav")
    res = await create_event(_req(), EventCreate(
        summary="Temp", dtstart="2026-06-10T14:00:00Z", calendar_href=cal_id))
    uid = res["uid"]
    calls.clear()
    rd = await delete_event(_req(), uid)
    assert rd["ok"] is True
    assert len(calls) == 1 and calls[0]["delete"] is True and calls[0]["uid"] == uid
