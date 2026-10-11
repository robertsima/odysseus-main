"""Capture: one line in, routed to the right system, exactly once.

`capture()` is the single entry point behind `POST /api/capture` and the `capture`
agent tool. It classifies the line (`src.capture_rules`, then the utility model only
when the rules cannot tell), asks a question only when two destinations are both
plausible, and writes to Todoist, the calendar, notes or outcomes through the code
those systems already use.

Exactly-once, and where it stops:

* A `capture_requests` row for (owner, key) is inserted `pending` before anything is
  written. The unique constraint means a second request with the same key waits for
  the first and returns its result instead of writing again.
* A retry of a `failed` row, or of a `pending` row left behind by a crash, runs the
  destination's duplicate check before it creates anything. An external create that
  timed out after succeeding is therefore found by the retry, not repeated.
* There is no transaction across Todoist and Odysseus. A crash between a Todoist
  create and the ledger write is reconciled by that duplicate check, never rolled back.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from core import database as cdb
from core.database import CaptureRequest
from src import capture_rules as rules
from src.todoist_calendar_sync import TOKEN_ENV_VAR, _run_td, _tasks_from_json

logger = logging.getLogger(__name__)

DESTINATIONS = ("todoist", "calendar", "note", "outcome")
MOVE_WINDOW = timedelta(minutes=30)
PENDING_STALE = timedelta(seconds=120)  # a pending row older than this belongs to a crashed request
PENDING_WAIT = 30.0                     # seconds a duplicate request waits for the one in flight
DEFAULT_FOLLOW_UP = timedelta(days=3)
REMINDER_HOUR = 9                       # a reminder with a date and no time pings at 09:00 local
MAX_TEXT = 1000
_MODEL_CONFIDENT = 0.6

_SQUASH = re.compile(r"\s+")


# ── clock and time zone (tests pin `_utcnow`) ────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _now_local() -> datetime:
    """The person's wall clock, naive."""
    from src.user_time import now_user_local

    return now_user_local(_utcnow()).replace(tzinfo=None)


def _apply_time_context(tz: Optional[str], tz_offset: Optional[int]) -> None:
    if not tz and tz_offset is None:
        return
    from src.user_time import clear_user_time_context, set_user_tz_name, set_user_tz_offset

    clear_user_time_context()
    if tz_offset is not None:
        set_user_tz_offset(tz_offset)
    if tz:
        set_user_tz_name(tz)


@contextmanager
def _floating_time():
    """Run with no time zone set, so a date-only value stays on that calendar day.
    manage_calendar tags every value with the person's offset, which moves an all-day event east of UTC."""
    from src.user_time import (clear_user_time_context, get_user_tz_name, get_user_tz_offset,
                               set_user_tz_name, set_user_tz_offset)

    offset, name = get_user_tz_offset(), get_user_tz_name()
    clear_user_time_context()
    try:
        yield
    finally:
        set_user_tz_offset(offset)
        set_user_tz_name(name)


def _aware_iso(local: datetime) -> str:
    """Local wall time with the person's offset, through the same parser the notes and calendar tools use."""
    from routes.calendar_routes import parse_due_for_user

    return parse_due_for_user(local.strftime("%Y-%m-%dT%H:%M:%S"))


def _utc_naive(local: datetime) -> str:
    moment = datetime.fromisoformat(_aware_iso(local))
    return moment.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def _reminder_local(when: rules.When) -> datetime:
    return when.start if when.has_time else when.start.replace(hour=REMINDER_HOUR, minute=0)


# ── results ──────────────────────────────────────────────────────────────────

def _failed(error: str, code: str = "error", destination: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    return {"status": "failed", "code": code, "destination": destination, "error": error, **extra}


def _ok(status: str, destination: str, ref: Optional[str], open_url: Optional[str], plan: Dict[str, Any],
        *, due: Optional[str] = None, refs: Optional[List[Dict[str, str]]] = None, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"status": status, "destination": destination, "ref": ref, "open_url": open_url,
                           "title": plan.get("title"), "due": due, "created_refs": refs or []}
    if plan.get("fallback_from"):
        out["fallback_from"] = plan["fallback_from"]
        out["message"] = (f"Saved as a note reminder: {plan['fallback_from'].capitalize()} is not available "
                          "for this account.")
    if plan.get("follow_up_defaulted"):
        out["follow_up_defaulted"] = True
    out.update(extra)
    return out


# ── the model, used only when the rules cannot tell ──────────────────────────

async def _utility(owner: str, system: str, user: str) -> Optional[str]:
    from src.endpoint_resolver import resolve_endpoint
    from src.llm_core import llm_call_async
    from src.text_helpers import strip_think

    url, model, headers = resolve_endpoint("utility", owner=owner or None)
    if not url:
        url, model, headers = resolve_endpoint("default", owner=owner or None)
    if not url or not model:
        return None
    try:
        raw = await llm_call_async(
            url=url, model=model, headers=headers, temperature=0.0, max_tokens=200, timeout=20,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    except Exception as exc:  # noqa: BLE001
        logger.info("capture: utility model failed: %s", exc)
        return None
    cleaned = strip_think(raw or "", prose=False, prompt_echo=True)
    match = re.search(r"\{[\s\S]*\}", cleaned)
    return match.group(0) if match else None


_MODEL_ROUTES = {"task": "task", "calendar": "calendar", "outcome": "outcome", "note": "task", "todoist": "task"}


async def _ask_model(owner: str, text: str, now: datetime) -> Tuple[str, Optional[str]]:
    """Return (route, other). `other` is set only when the model is unsure between two routes.
    Falls back to a plain task when there is no model or its answer is unusable."""
    system = (
        "Decide where one line a person typed belongs. Reply with JSON only: "
        '{"destination": "task|calendar|outcome", "confidence": 0.0-1.0, "alternative": "task|calendar|outcome|none"}. '
        "task = something to do or remember; calendar = a fixed appointment at a date or time; "
        "outcome = a multi-step goal or something being waited on. alternative is the second most likely "
        f"destination. Today is {now:%A %Y-%m-%d}.")
    raw = await _utility(owner, system, text)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    route = _MODEL_ROUTES.get(str(data.get("destination") or "").lower())
    if route is None:
        return "task", None
    alt = _MODEL_ROUTES.get(str(data.get("alternative") or "").lower())
    try:
        confidence = float(data.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0
    if confidence < _MODEL_CONFIDENT and alt and alt != route:
        return route, alt
    return route, None


# ── planning: what to do with the line ───────────────────────────────────────

def _todoist_ok(owner: str) -> bool:
    """Todoist is one shared account: only the admin or the single user may write to it."""
    if not os.environ.get(TOKEN_ENV_VAR):
        return False
    from src.tool_security import owner_is_admin_or_single_user

    return bool(owner_is_admin_or_single_user(owner))


def _task_target(owner: str) -> Tuple[str, Optional[str]]:
    return ("todoist", None) if _todoist_ok(owner) else ("note", "todoist")


def _option(owner: str, route: str, when: Optional[rules.When]) -> Dict[str, Any]:
    if route == "calendar":
        return {"label": "Put it on the calendar", "destination": "calendar",
                "due": when.iso() if when else None, "needs_due": when is None}
    if route == "outcome":
        return {"label": "Track it as a goal", "destination": "outcome", "due": when.iso() if when else None}
    dest, _ = _task_target(owner)
    label = "Add it as a task" if dest == "todoist" else "Save it as a reminder note"
    return {"label": label, "destination": dest, "due": when.iso() if when else None}


def _ask(owner: str, question: str, routes: Tuple[str, ...], when: Optional[rules.When]) -> Dict[str, Any]:
    return {"status": "needs_clarification", "question": question,
            "options": [_option(owner, r, when) for r in routes]}


async def _plan(owner: str, text: str, destination: Optional[str], due: Optional[str],
                now: datetime) -> Dict[str, Any]:
    """Return the plan to execute, or a finished response (a question, or a failure)."""
    d = rules.classify(text, now)
    when = d.when
    if due:
        when = rules.resolve_phrase(due, now)
        if when is None:
            return _failed(f"Could not read the date {due!r}.", "invalid")
    title = d.title or text
    plan: Dict[str, Any] = {"title": title[:300], "forced": destination, "reminder": d.reminder,
                            "multi_step": d.multi_step, "waiting_on": None, "kind": "obligation",
                            "due": when.iso() if when else None, "has_time": bool(when and when.has_time)}

    dest = destination
    if dest is None:
        route = d.route
        if route is None:
            route, other = await _ask_model(owner, text, now)
            if other:
                return _ask(owner, "Where should this go?", (route, other), when)
        if route == "clarify":
            return _ask(owner, d.question or "Where should this go?", d.alternatives, when)
        if route == "calendar":
            dest = "calendar"
        elif route == "waiting":
            dest = "outcome"
        elif route == "outcome":
            dest = "outcome"
        else:  # a task; a timed reminder is a note so the existing ping loop delivers it
            if d.reminder and when and when.has_time:
                dest = "note"
            else:
                dest, plan["fallback_from"] = _task_target(owner)
                if plan["fallback_from"] is None:
                    del plan["fallback_from"]
    if dest not in DESTINATIONS:
        return _failed(f"destination must be one of {', '.join(DESTINATIONS)}.", "invalid")

    if dest == "outcome":
        if d.route == "waiting" and d.waiting_on:
            plan["waiting_on"] = d.waiting_on
            follow = _reminder_local(when) if when else (now + DEFAULT_FOLLOW_UP).replace(
                hour=REMINDER_HOUR, minute=0, second=0, microsecond=0)
            plan["follow_up"] = follow.strftime("%Y-%m-%dT%H:%M:%S")
            if when is None:
                plan["follow_up_defaulted"] = True
            plan["due"] = None
        if d.multi_step:
            plan["kind"] = "project"
    elif dest == "calendar" and when is None:
        parsed = await _quick_parse(owner, text)
        if parsed is None:
            return _failed("A calendar entry needs a date or time. Send one in `due`.", "needs_date", "calendar")
        plan.update(parsed)
    return {**plan, "destination": dest}


async def _quick_parse(owner: str, text: str) -> Optional[Dict[str, Any]]:
    """Ask the calendar's own parser (the logic behind /api/calendar/quick-parse) for a start time."""
    from routes.calendar_routes import quick_parse_event
    from src.user_time import get_user_tz_name, get_user_tz_offset

    res = await quick_parse_event(owner, text, tz=get_user_tz_name() or "", tz_offset=get_user_tz_offset())
    event = res.get("event") if res.get("ok") else None
    if not event or not event.get("dtstart"):
        return None
    return {"title": (event.get("summary") or text)[:300], "due": event["dtstart"],
            "has_time": not event.get("all_day"), "dtend": event.get("dtend"),
            "location": event.get("location") or ""}


# ── ledger ───────────────────────────────────────────────────────────────────

def _row_dict(row: CaptureRequest) -> Dict[str, Any]:
    def load(raw: Optional[str]) -> Optional[Dict[str, Any]]:
        try:
            value = json.loads(raw) if raw else None
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    return {"id": row.id, "owner": row.owner, "key": row.key, "status": row.status, "text": row.text,
            "plan": load(row.request_json), "result": load(row.result_json), "attempts": row.attempts,
            "created_at": row.created_at, "updated_at": row.updated_at}


def _get_row(owner: str, key: str) -> Optional[Dict[str, Any]]:
    db = cdb.SessionLocal()
    try:
        row = db.query(CaptureRequest).filter(CaptureRequest.owner == owner, CaptureRequest.key == key).first()
        return _row_dict(row) if row else None
    finally:
        db.close()


def _insert_pending(owner: str, key: str, text: str, plan: Dict[str, Any]) -> bool:
    """Claim (owner, key). False means another request holds it."""
    db = cdb.SessionLocal()
    try:
        now = _utcnow()
        db.add(CaptureRequest(id=uuid.uuid4().hex, owner=owner, key=key, status="pending", text=text,
                              request_json=json.dumps(plan), attempts=1, created_at=now, updated_at=now))
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False
    finally:
        db.close()


def _claim(row: Dict[str, Any], from_statuses: Tuple[str, ...], plan: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """Take a failed, stale-pending or done row for one more attempt. Only one caller wins."""
    db = cdb.SessionLocal()
    try:
        values: Dict[str, Any] = {"status": "pending", "attempts": row["attempts"] + 1, "updated_at": _utcnow()}
        if plan is not None:
            values["request_json"] = json.dumps(plan)
        done = db.execute(update(CaptureRequest).where(
            CaptureRequest.id == row["id"], CaptureRequest.attempts == row["attempts"],
            CaptureRequest.status.in_(from_statuses)).values(**values))
        db.commit()
        return row["attempts"] + 1 if done.rowcount == 1 else None
    finally:
        db.close()


def _finish(row_id: str, attempts: int, status: str, result: Dict[str, Any], plan: Optional[Dict[str, Any]] = None) -> None:
    db = cdb.SessionLocal()
    try:
        values: Dict[str, Any] = {"status": status, "result_json": json.dumps(result, default=str),
                                  "updated_at": _utcnow()}
        if plan is not None:
            values["request_json"] = json.dumps(plan)
        db.execute(update(CaptureRequest).where(
            CaptureRequest.id == row_id, CaptureRequest.attempts == attempts).values(**values))
        db.commit()
    finally:
        db.close()


def derive_key(text: str, now: datetime) -> str:
    basis = f"{_SQUASH.sub(' ', text).strip().lower()}|{now.date().isoformat()}"
    return "auto-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


# ── destinations ─────────────────────────────────────────────────────────────

async def _td(args: List[str]) -> Tuple[int, str, str]:
    return await _run_td(args)


def _task_id(output: str) -> Optional[str]:
    try:
        data = json.loads(output or "")
    except ValueError:
        return None
    if isinstance(data, dict):
        for holder in (data, data.get("task") if isinstance(data.get("task"), dict) else {},
                       (data.get("results") or [{}])[0] if isinstance(data.get("results"), list) and data.get("results") else {}):
            if isinstance(holder, dict) and holder.get("id"):
                return str(holder["id"])
    return None


async def _find_todoist(title: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """The open task whose normalised content matches `title`, or the reason the check could not run."""
    code, out, err = await _td(["task", "list", "--json"])
    if code != 0:
        return None, err or out or "Todoist did not answer"
    try:
        tasks = _tasks_from_json(out)
    except ValueError as exc:
        return None, f"Todoist returned invalid JSON: {exc}"
    want = rules.normalize_title(title)
    for task in tasks:
        if rules.normalize_title(str(task.get("content") or "")) == want:
            return task, None
    return None, None


def _todoist_url(task: Dict[str, Any]) -> str:
    return str(task.get("url") or f"https://app.todoist.com/app/task/{task.get('id')}")


async def _do_todoist(owner: str, plan: Dict[str, Any], *, retry: bool, actor: str) -> Dict[str, Any]:
    title = plan["title"]
    found, problem = await _find_todoist(title)
    if problem and retry:
        # An earlier attempt may have created the task; without the check a second create could duplicate it.
        return _failed(f"Could not check Todoist for an existing copy ({problem}); nothing was created.",
                       "check_failed", "todoist")
    if found:
        return _ok("duplicate", "todoist", str(found.get("id")), _todoist_url(found), plan, due=plan.get("due"))
    args = ["task", "add", title] + (["--due", plan["due"]] if plan.get("due") else []) + ["--json"]
    code, out, err = await _td(args)
    if code != 0:
        return _failed(err or out or "Todoist could not create the task.", "create_failed", "todoist")
    task_id = _task_id(out)
    if not task_id:
        made, _ = await _find_todoist(title)
        task_id = str(made["id"]) if made and made.get("id") else None
    url = f"https://app.todoist.com/app/task/{task_id}" if task_id else None
    refs = [{"destination": "todoist", "ref": task_id}] if task_id else []
    return _ok("created", "todoist", task_id, url, plan, due=plan.get("due"), refs=refs)


def _event_args(plan: Dict[str, Any]) -> Dict[str, Any]:
    args: Dict[str, Any] = {"action": "create_event", "summary": plan["title"], "dtstart": plan["due"],
                            "all_day": not plan.get("has_time", True)}
    if plan.get("dtend"):
        args["dtend"] = plan["dtend"]
    if plan.get("location"):
        args["location"] = plan["location"]
    return args


async def _do_calendar(owner: str, plan: Dict[str, Any], *, retry: bool, actor: str) -> Dict[str, Any]:
    from src.tools.calendar import do_manage_calendar

    if not plan.get("due"):
        return _failed("A calendar entry needs a date or time.", "needs_date", "calendar")
    args = _event_args(plan)
    if args["all_day"]:
        with _floating_time():
            res = await do_manage_calendar(json.dumps(args), owner=owner)
    else:
        res = await do_manage_calendar(json.dumps(args), owner=owner)
    if res.get("exit_code") != 0 or not res.get("uid"):
        return _failed(res.get("error") or "The calendar could not create the event.", "create_failed", "calendar")
    uid = str(res["uid"])
    status = "duplicate" if res.get("duplicate") else "created"
    refs = [] if status == "duplicate" else [{"destination": "calendar", "ref": uid}]
    return _ok(status, "calendar", uid, f"/#event-{uid}", plan, due=plan["due"], refs=refs)


def _find_note(owner: str, title: str, due_iso: Optional[str]) -> Optional[Any]:
    from src import notes_store

    want = rules.normalize_title(title)
    for note in notes_store.STORE.list(owner, archived=False):
        if rules.normalize_title(note.title or "") == want and (note.due_date or None) == (due_iso or None):
            return note
    return None


async def _do_note(owner: str, plan: Dict[str, Any], *, retry: bool, actor: str,
                   title: Optional[str] = None, due_local: Optional[str] = None, content: Optional[str] = None,
                   ) -> Dict[str, Any]:
    from src.tools import notes as notes_tool

    title = title or plan["title"]
    due_text = due_local if due_local is not None else plan.get("due")
    due_iso = None
    if due_text:
        when = rules.resolve_phrase(due_text, _now_local())
        due_iso = _aware_iso(_reminder_local(when)) if when else due_text
    existing = _find_note(owner, title, due_iso)
    if existing is not None:
        return _ok("duplicate", "note", existing.id, f"/#open=notes&note={existing.id}", plan, due=due_iso)
    args: Dict[str, Any] = {"action": "add", "title": title}
    if due_iso:
        args["due_date"] = due_iso
    if content:
        args["content"] = content
    res = await notes_tool.do_manage_notes(json.dumps(args), owner=owner)
    if res.get("exit_code") != 0 or not res.get("note_id"):
        return _failed(res.get("error") or "The note could not be saved.", "create_failed", "note")
    note_id = str(res["note_id"])
    status = "duplicate" if res.get("duplicate") else "created"
    refs = [] if status == "duplicate" else [{"destination": "note", "ref": note_id}]
    return _ok(status, "note", note_id, f"/#open=notes&note={note_id}", plan, due=due_iso, refs=refs)


def _open_outcome_with_title(owner: str, title: str) -> Optional[Dict[str, Any]]:
    from src import outcomes

    want = rules.normalize_title(title)
    for outcome in outcomes.list_for(owner, ["open", "active", "waiting", "blocked"]):
        if rules.normalize_title(outcome["title"]) == want:
            return outcomes.get(owner, outcome["id"])
    return None


async def _do_outcome(owner: str, plan: Dict[str, Any], *, retry: bool, actor: str) -> Dict[str, Any]:
    from src import outcomes

    title, waiting_on = plan["title"], plan.get("waiting_on")
    outcome = _open_outcome_with_title(owner, title)
    refs: List[Dict[str, str]] = []
    if outcome is None:
        fields: Dict[str, Any] = {"kind": plan.get("kind") or "obligation", "source": "capture", "actor": actor,
                                  "state": "waiting" if waiting_on else "open"}
        if waiting_on:
            fields.update(waiting_on=waiting_on, follow_up_at=_utc_naive(datetime.fromisoformat(plan["follow_up"])))
        if plan.get("due") and not waiting_on:
            when = rules.resolve_phrase(plan["due"], _now_local())
            if when:
                fields["due_at"] = _utc_naive(when.start)
        made = outcomes.create(owner, title, **fields)
        if not made.get("ok"):
            return _failed(made.get("error") or "The outcome could not be created.", "create_failed", "outcome")
        outcome = made["outcome"]
        refs.append({"destination": "outcome", "ref": outcome["id"]})
    created_any = bool(refs)

    if waiting_on and not any(link["kind"] == "note" for link in outcome.get("links") or []):
        note = await _do_note(owner, plan, retry=retry, actor=actor, title=f"Follow up: {title}"[:300],
                              due_local=plan["follow_up"], content=f"Waiting on {waiting_on}.")
        if note["status"] == "failed":
            return _failed(f"The outcome exists but its follow-up reminder failed: {note['error']}",
                           "partial", "outcome", outcome_id=outcome["id"])
        outcomes.add_link(owner, outcome["id"], "note", note["ref"], label="Follow-up reminder", actor=actor)
        refs.extend(note["created_refs"])
        created_any = created_any or note["status"] == "created"
    return _ok("created" if created_any else "duplicate", "outcome", outcome["id"], None, plan,
               due=plan.get("follow_up") or plan.get("due"), refs=refs, outcome_id=outcome["id"])


async def _link_project(owner: str, plan: Dict[str, Any], result: Dict[str, Any], actor: str) -> None:
    """A Todoist task or event made from a multi-step sentence gets an outcome that links to it."""
    from src import outcomes

    kind = {"todoist": "todoist_task", "calendar": "calendar_event"}.get(result["destination"])
    if not kind or not result.get("ref") or not plan.get("multi_step"):
        return
    existing = outcomes.find_by_link(owner, kind, result["ref"])
    if existing:
        result["outcome_id"] = existing[0]["id"]
        return
    made = outcomes.create(owner, plan["title"], kind="project", source="capture", actor=actor,
                           links=[{"kind": kind, "ref": result["ref"], "label": plan["title"][:300]}])
    if made.get("ok"):
        result["outcome_id"] = made["outcome"]["id"]
        result["created_refs"].append({"destination": "outcome", "ref": made["outcome"]["id"]})


_HANDLERS = {"todoist": _do_todoist, "calendar": _do_calendar, "note": _do_note, "outcome": _do_outcome}


async def _execute(owner: str, plan: Dict[str, Any], *, retry: bool, actor: str) -> Dict[str, Any]:
    dest = plan["destination"]
    if dest == "todoist" and not _todoist_ok(owner):
        plan = {**plan, "destination": "note", "fallback_from": "todoist"}
        dest = "note"
    result = await _HANDLERS[dest](owner, plan, retry=retry, actor=actor)
    if result["status"] in ("created", "duplicate"):
        await _link_project(owner, plan, result, actor)
    return result


# ── capture ──────────────────────────────────────────────────────────────────

async def _wait_for_result(owner: str, key: str) -> Optional[Dict[str, Any]]:
    """Poll while another request holds the key. Returns its row once it is no longer pending."""
    deadline = _utcnow() + timedelta(seconds=PENDING_WAIT)
    while True:
        row = _get_row(owner, key)
        if row is None or row["status"] != "pending":
            return row
        if _utcnow() - row["updated_at"] >= PENDING_STALE or _utcnow() >= deadline:
            return row
        await asyncio.sleep(0.05)


async def _run(owner: str, row: Dict[str, Any], plan: Dict[str, Any], *, retry: bool, actor: str) -> Dict[str, Any]:
    try:
        result = await _execute(owner, plan, retry=retry, actor=actor)
    except Exception as exc:  # noqa: BLE001
        logger.warning("capture failed: %s", exc, exc_info=True)
        result = _failed(str(exc)[:300] or "The capture failed.", "error", plan.get("destination"))
    result["key"] = row["key"]
    result.setdefault("title", plan.get("title"))
    _finish(row["id"], row["attempts"], "done" if result["status"] in ("created", "duplicate") else "failed", result)
    return result


async def capture(owner: str, text: str, *, idempotency_key: Optional[str] = None, destination: Optional[str] = None,
                  due: Optional[str] = None, actor: str = "person", tz: Optional[str] = None,
                  tz_offset: Optional[int] = None) -> Dict[str, Any]:
    """Route one line to Todoist, the calendar, a note or an outcome, once."""
    owner = (owner or "").strip()
    text = _SQUASH.sub(" ", text or "").strip()[:MAX_TEXT]
    if not owner:
        return _failed("No signed-in user.", "invalid")
    if not text:
        return _failed("Nothing to capture.", "invalid")
    if destination is not None and destination not in DESTINATIONS:
        return _failed(f"destination must be one of {', '.join(DESTINATIONS)}.", "invalid")
    _apply_time_context(tz, tz_offset)
    now = _now_local()
    key = (idempotency_key or "").strip()[:200] or derive_key(text, now)

    for _ in range(5):
        row = _get_row(owner, key)
        if row is None:
            plan = await _plan(owner, text, destination, due, now)
            if "status" in plan:
                return plan  # a question, or a failure that wrote nothing
            if not _insert_pending(owner, key, text, plan):
                continue  # lost the race for the key; read the winner's row
            return await _run(owner, _get_row(owner, key), plan, retry=False, actor=actor)
        if row["status"] == "done" and row["result"]:
            return {**row["result"], "replayed": True}
        if row["status"] == "pending" and _utcnow() - row["updated_at"] < PENDING_STALE:
            row = await _wait_for_result(owner, key)
            if row is not None and row["status"] == "done" and row["result"]:
                return {**row["result"], "replayed": True}
            if row is not None and row["status"] == "pending" and _utcnow() - row["updated_at"] < PENDING_STALE:
                return _failed("This capture is still being processed. Try again shortly.", "busy")
            continue
        # failed, or pending from a request that never finished: take it over and reconcile first
        plan = row["plan"]
        if not plan or (destination and plan.get("forced") != destination):
            plan = await _plan(owner, text, destination, due, now)
            if "status" in plan:
                return plan
        claimed = _claim(row, ("failed", "pending"), plan)
        if claimed is None:
            continue
        return await _run(owner, {**row, "attempts": claimed}, plan, retry=True, actor=actor)
    return _failed("This capture is busy. Try again shortly.", "busy")


# ── move ─────────────────────────────────────────────────────────────────────

def _refused(code: str, error: str) -> Dict[str, Any]:
    return {"status": "refused", "code": code, "error": error}


async def _remove(owner: str, item: Dict[str, str], actor: str) -> bool:
    dest, ref = item.get("destination"), item.get("ref")
    if not ref:
        return False
    try:
        if dest == "todoist":
            code, _, _ = await _td(["task", "delete", ref, "--yes"])
            return code == 0
        if dest == "calendar":
            from src.tools.calendar import do_manage_calendar

            res = await do_manage_calendar(json.dumps({"action": "delete_event", "uid": ref}), owner=owner)
            return res.get("exit_code") == 0
        if dest == "note":
            from src.tools import notes as notes_tool

            res = await notes_tool.do_manage_notes(json.dumps({"action": "delete", "id": ref}), owner=owner)
            return res.get("exit_code") == 0
        if dest == "outcome":
            from src import outcomes

            return bool(outcomes.set_state(owner, ref, "dropped", "person").get("ok"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("capture: could not remove %s %s: %s", dest, ref, exc)
    return False


async def move(owner: str, key: str, to: str, *, actor: str = "person", tz: Optional[str] = None,
               tz_offset: Optional[int] = None) -> Dict[str, Any]:
    """Re-route a capture made in the last 30 minutes. The old item is removed only if this capture created it."""
    owner, key = (owner or "").strip(), (key or "").strip()
    if to not in DESTINATIONS:
        return _refused("invalid", f"to must be one of {', '.join(DESTINATIONS)}.")
    row = _get_row(owner, key) if owner and key else None
    if row is None:
        return _refused("not_found", "No capture with that key.")
    old = row["result"] or {}
    if row["status"] != "done" or old.get("status") not in ("created", "duplicate"):
        return _refused("not_ready", "That capture has not finished.")
    if old["status"] == "duplicate":
        return _refused("duplicate", "That capture matched an item that already existed, so it is not moved.")
    if _utcnow() - row["created_at"] > MOVE_WINDOW:
        return _refused("too_late", "A capture can only be moved within 30 minutes.")
    target = "note" if to == "todoist" and not _todoist_ok(owner) else to
    if target == old.get("destination"):
        return _refused("invalid", "The capture is already there.")
    _apply_time_context(tz, tz_offset)

    claimed = _claim(row, ("done",))
    if claimed is None:
        return _refused("busy", "That capture is being changed. Try again shortly.")
    plan = {**(row["plan"] or {"title": old.get("title") or row["text"]}), "destination": target, "forced": to}
    plan.pop("fallback_from", None)
    if target == "note" and to == "todoist":
        plan["fallback_from"] = "todoist"
    if target == "calendar" and not plan.get("due"):
        parsed = await _quick_parse(owner, row["text"])
        plan.update(parsed or {})
    try:
        new = await _execute(owner, plan, retry=False, actor=actor)
    except Exception as exc:  # noqa: BLE001
        logger.warning("capture move failed: %s", exc, exc_info=True)
        new = _failed(str(exc)[:300], "error", target)
    if new["status"] == "failed":
        _finish(row["id"], claimed, "done", old)  # the original stays as it was
        return {**new, "key": key}

    removed = [await _remove(owner, item, actor) for item in old.get("created_refs") or []]
    new["moved_from"] = {"destination": old.get("destination"), "ref": old.get("ref"), "removed": all(removed)}
    new["key"] = key
    _finish(row["id"], claimed, "done", new, plan)
    return new
