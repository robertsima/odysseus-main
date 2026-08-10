"""Todoist -> local calendar sync.

Todoist remains the source of truth. We cache dated Todoist tasks into the
SQLite calendar tables so the existing Odysseus calendar UI can render them
alongside local and CalDAV events.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

TOKEN_ENV_VAR = "TODOIST_API_TOKEN"
TODOIST_CALENDAR_COLOR = "#e44332"


def _todoist_calendar_id(owner: str) -> str:
    digest = hashlib.sha256((owner or "").encode("utf-8")).hexdigest()[:24]
    return f"todoist-{digest}"


def _todoist_event_uid(task_id: str) -> str:
    return f"todoist-task-{task_id}"


def _redact(text: str) -> str:
    token = os.environ.get(TOKEN_ENV_VAR, "")
    return text.replace(token, "[REDACTED]") if token else text


async def _run_td(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    if not os.environ.get(TOKEN_ENV_VAR):
        return 1, "", f"{TOKEN_ENV_VAR} is not configured"
    td_path = shutil.which("td")
    if not td_path:
        return 1, "", "Todoist CLI executable 'td' was not found in PATH"
    try:
        proc = await asyncio.create_subprocess_exec(
            td_path,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return 1, "", f"Todoist CLI timed out after {timeout} seconds"
    except OSError as exc:
        return 1, "", f"Failed to start Todoist CLI: {_redact(str(exc))}"
    return (
        proc.returncode or 0,
        _redact(stdout.decode(errors="replace").strip()),
        _redact(stderr.decode(errors="replace").strip()),
    )


def _tasks_from_json(raw: str) -> list[dict[str, Any]]:
    data = json.loads(raw or "[]")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("results", "tasks", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _parse_todoist_date(value: str) -> tuple[datetime, bool, bool] | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        if len(value) == 10:
            return datetime.fromisoformat(value), True, False
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None), False, True
        return parsed, False, False
    except ValueError:
        return None


def _task_due(task: dict[str, Any]) -> tuple[datetime, datetime, bool, bool] | None:
    due = task.get("due") if isinstance(task.get("due"), dict) else {}
    deadline = task.get("deadline") if isinstance(task.get("deadline"), dict) else {}
    raw = due.get("date") or deadline.get("date") or ""
    parsed = _parse_todoist_date(raw)
    if not parsed:
        return None
    start, all_day, is_utc = parsed
    end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
    return start, end, all_day, is_utc


def _description(task: dict[str, Any]) -> str:
    parts = []
    if task.get("description"):
        parts.append(str(task.get("description")))
    if task.get("url"):
        parts.append(f"Todoist: {task.get('url')}")
    labels = task.get("labels")
    if isinstance(labels, list) and labels:
        parts.append("Labels: " + ", ".join(str(label) for label in labels))
    return "\n\n".join(parts)


def _importance(priority: Any) -> str:
    try:
        value = int(priority)
    except (TypeError, ValueError):
        return "normal"
    if value >= 4:
        return "critical"
    if value == 3:
        return "high"
    if value <= 1:
        return "low"
    return "normal"


async def todoist_status() -> dict[str, Any]:
    if not os.environ.get(TOKEN_ENV_VAR):
        return {"configured": False, "ok": False, "error": f"{TOKEN_ENV_VAR} is not configured"}
    if not shutil.which("td"):
        return {"configured": True, "ok": False, "error": "Todoist CLI executable 'td' was not found in PATH"}
    code, stdout, stderr = await _run_td(["auth", "status"], timeout=15)
    if code != 0:
        return {"configured": True, "ok": False, "error": stderr or stdout or "Todoist auth check failed"}
    return {"configured": True, "ok": True, "status": stdout}


async def sync_todoist_calendar(owner: str) -> dict[str, Any]:
    """Pull active dated Todoist tasks into the owner's calendar cache."""
    if not os.environ.get(TOKEN_ENV_VAR):
        return {"calendars": 0, "events": 0, "deleted": 0, "errors": [], "skipped": True}

    code, stdout, stderr = await _run_td(["task", "list", "--json"], timeout=60)
    if code != 0:
        return {"calendars": 0, "events": 0, "deleted": 0, "errors": [stderr or stdout or "Todoist sync failed"]}

    try:
        tasks = _tasks_from_json(stdout)
    except json.JSONDecodeError as exc:
        return {"calendars": 0, "events": 0, "deleted": 0, "errors": [f"Todoist returned invalid JSON: {exc}"]}

    from core.database import CalendarCal, CalendarEvent, SessionLocal

    cal_id = _todoist_calendar_id(owner)
    seen_uids: set[str] = set()
    upserts = 0
    db = SessionLocal()
    try:
        cal = db.query(CalendarCal).filter(
            CalendarCal.id == cal_id,
            CalendarCal.owner == owner,
        ).first()
        if not cal:
            cal = CalendarCal(
                id=cal_id,
                owner=owner,
                name="Todoist",
                color=TODOIST_CALENDAR_COLOR,
                source="todoist",
            )
            db.add(cal)
            db.commit()
        else:
            changed = False
            if cal.name != "Todoist":
                cal.name = "Todoist"
                changed = True
            if not cal.color:
                cal.color = TODOIST_CALENDAR_COLOR
                changed = True
            if cal.source != "todoist":
                cal.source = "todoist"
                changed = True
            if changed:
                db.commit()

        for task in tasks:
            task_id = str(task.get("id") or "").strip()
            if not task_id:
                continue
            due = _task_due(task)
            if not due:
                continue
            start, end, all_day, is_utc = due
            uid = _todoist_event_uid(task_id)
            seen_uids.add(uid)
            ev = db.query(CalendarEvent).filter(
                CalendarEvent.uid == uid,
                CalendarEvent.calendar_id == cal_id,
            ).first()
            if not ev:
                ev = CalendarEvent(uid=uid, calendar_id=cal_id, origin="todoist")
                db.add(ev)
            ev.summary = str(task.get("content") or "Todoist task")
            ev.description = _description(task)
            ev.location = ""
            ev.dtstart = start
            ev.dtend = end
            ev.all_day = all_day
            ev.is_utc = is_utc and not all_day
            ev.rrule = ""
            ev.color = None
            ev.status = "confirmed"
            ev.importance = _importance(task.get("priority"))
            ev.event_type = "todoist"
            ev.origin = "todoist"
            ev.remote_href = str(task.get("url") or task_id)
            ev.remote_etag = None
            ev.caldav_sync_pending = None
            upserts += 1

        stale_query = db.query(CalendarEvent).filter(
            CalendarEvent.calendar_id == cal_id,
            CalendarEvent.origin == "todoist",
        )
        if seen_uids:
            stale_query = stale_query.filter(~CalendarEvent.uid.in_(seen_uids))
        stale = stale_query.all()
        deleted = len(stale)
        for ev in stale:
            db.delete(ev)
        db.commit()
        return {"calendars": 1, "events": upserts, "deleted": deleted, "errors": []}
    except Exception as exc:
        db.rollback()
        logger.exception("Todoist calendar sync failed")
        return {"calendars": 0, "events": 0, "deleted": 0, "errors": [str(exc)[:200]]}
    finally:
        db.close()
