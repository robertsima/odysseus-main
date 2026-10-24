"""Today: a short, explained list of what needs the person now.

Every source is read live and nothing is copied. A source that fails or runs
past its timeout is named in `degraded`; the others still render. The list is
a recommendation, never a backlog: at most `TODAY_CAP` items that need the
person today and `NEXT_CAP` that are worth starting or resuming.

Ranking is deterministic. Tiers, in order: overdue, due today, blocked on the
person, follow-ups that are due. The rest ("next") orders by the person's
priority, then active work before resumable runs before open outcomes, then
the most recently touched. Items cut from `today` by the cap move to the head
of `next`, so nothing late disappears.

Two rules hold for every action:

* Nothing here changes a due date or priority. Defer and wait write
  `snoozed_until` or the waiting fields on an outcome, never on the source.
* An item reached through two sources counts once, shown as the outcome.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone, tzinfo
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from src import blockers, outcomes as store

logger = logging.getLogger(__name__)

TODAY_CAP = 5
NEXT_CAP = 3
SOURCE_TIMEOUT = 3.0
TODOIST_TIMEOUT = 6.0
RESUME_WAIT = 2.0  # how long a resume waits to hear a launch refusal before it reports "started"
RUN_WINDOW = timedelta(days=7)
RESUMABLE_RUN_STATUSES = ("interrupted", "partial")
SNOOZE_HOUR = 9
WAIT_DAYS = 3
ACTIONS = ("start", "resume", "defer", "wait", "done", "open")
_OPEN_STATES = ("open", "active", "waiting", "blocked")
# What `_run_td` says when Todoist is not set up. Not a failure, so not "degraded".
_TODOIST_ABSENT = ("is not configured", "was not found in PATH")
_BACKGROUND: set = set()

TIER_OVERDUE, TIER_DUE, TIER_BLOCKED, TIER_FOLLOW_UP, TIER_REST = range(5)
GROUP_ACTIVE, GROUP_RUN, GROUP_OPEN = range(3)


def _utcnow() -> datetime:
    """Naive UTC, like the stored timestamps. Tests replace this."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ActionError(Exception):
    """A refused action: `status` is the HTTP status the route answers with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class _Absent(Exception):
    """A source that is not set up for this person."""


# ── time ─────────────────────────────────────────────────────────────────────

def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _iso_z(dt: Optional[datetime]) -> Optional[str]:
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _clock(dt: datetime) -> str:
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


@dataclass
class Ctx:
    owner: str
    notes_owner: Optional[str]
    todoist: bool
    now: datetime  # aware UTC
    tz: tzinfo

    @property
    def local_now(self) -> datetime:
        return self.now.astimezone(self.tz)

    @property
    def today(self) -> date:
        return self.local_now.date()

    def day_start(self, day: date) -> datetime:
        return datetime.combine(day, dtime.min, self.tz).astimezone(timezone.utc)

    @property
    def day_end(self) -> datetime:
        return self.day_start(self.today + timedelta(days=1))

    def at_hour(self, day: date) -> datetime:
        return datetime.combine(day, dtime(SNOOZE_HOUR), self.tz).astimezone(timezone.utc)


@dataclass
class Due:
    """When something is due: an instant (UTC) and the local calendar day it falls on."""
    at: datetime
    day: date
    all_day: bool


def _due_from_date(ctx: Ctx, day: date) -> Due:
    return Due(ctx.day_start(day), day, True)


def _due_from_instant(ctx: Ctx, at: datetime) -> Due:
    return Due(_utc(at), _utc(at).astimezone(ctx.tz).date(), False)


def _parse_due(ctx: Ctx, raw: Any) -> Optional[Due]:
    """A date ("2026-10-06"), a UTC instant ("...Z") or a floating time (read in the person's zone)."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            return _due_from_date(ctx, date.fromisoformat(text))
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ctx.tz)
    return _due_from_instant(ctx, parsed)


def _stored_due(ctx: Ctx, naive: Optional[datetime]) -> Optional[Due]:
    """An outcome's own date: naive UTC, and midnight means "that day"."""
    if naive is None:
        return None
    if naive.time() == dtime.min:
        return _due_from_date(ctx, naive.date())
    return _due_from_instant(ctx, naive)


def _day_label(ctx: Ctx, day: date) -> str:
    if 0 <= (ctx.today - day).days <= 6:
        return day.strftime("%a")
    return f"{day.strftime('%b')} {day.day}"


def _due_why(ctx: Ctx, due: Due, today_text: str) -> str:
    if due.day < ctx.today:
        return f"Overdue since {_day_label(ctx, due.day)}"
    if due.all_day:
        return f"{today_text} today"
    return f"{today_text} today at {_clock(due.at.astimezone(ctx.tz))}"


def _ago(ctx: Ctx, then: datetime) -> str:
    seconds = max(0, int((ctx.now - _utc(then)).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        n = seconds // 60
        return f"{n} minute{'s' if n != 1 else ''} ago"
    if seconds < 86400:
        n = seconds // 3600
        return f"{n} hour{'s' if n != 1 else ''} ago"
    if seconds < 2 * 86400:
        return "yesterday"
    return f"{seconds // 86400} days ago"


def _epoch(dt: Optional[datetime]) -> float:
    return _utc(dt).timestamp() if dt else 0.0


# ── candidate rows ───────────────────────────────────────────────────────────

@dataclass
class Row:
    item: Dict[str, Any]
    tier: int
    due: Optional[datetime] = None
    priority: int = 5
    group: int = GROUP_OPEN
    touched: float = 0.0
    today: bool = False  # belongs in the "today" section

    def sort_key(self) -> Tuple:
        return (self.tier, self.due or datetime.max.replace(tzinfo=timezone.utc), self.priority, self.group,
                -self.touched, self.item["key"])


def _item(key: str, title: str, why: str, source: str, *, due: Optional[str] = None, state: Optional[str] = None,
          blocker: Optional[dict] = None, actions: List[str], open_url: Optional[str] = None) -> Dict[str, Any]:
    ordered = [a for a in ACTIONS if a in actions and (a != "open" or open_url)]
    return {"key": key, "title": title, "why": why, "source": source, "due": due, "state": state,
            "blocker": blocker, "actions": ordered, "open_url": open_url}


# ── sources ──────────────────────────────────────────────────────────────────

def _todoist_priority(value: Any) -> int:
    """Todoist's 4 is its highest; ours is 1."""
    try:
        return 5 - int(value) if 1 <= int(value) <= 4 else 5
    except (TypeError, ValueError):
        return 5


def _task_list(raw: str) -> List[Dict[str, Any]]:
    data = json.loads(raw or "[]")
    if isinstance(data, dict):
        data = [row for value in data.values() if isinstance(value, list) for row in value]
    return [row for row in data if isinstance(row, dict) and row.get("id")]


async def _fetch_todoist(ctx: Ctx) -> List[Dict[str, Any]]:
    """Open Todoist tasks due today or earlier, live from the CLI."""
    if not ctx.todoist:
        raise _Absent()
    from src import todoist_calendar_sync as td

    code, stdout, stderr = await td._run_td(["today", "--json"], timeout=int(TODOIST_TIMEOUT))
    if code != 0:
        if any(marker in (stderr or "") for marker in _TODOIST_ABSENT):
            raise _Absent()
        raise RuntimeError(stderr or stdout or "td failed")
    tasks = []
    for task in _task_list(stdout):
        due = _parse_due(ctx, (task.get("due") or {}).get("date") if isinstance(task.get("due"), dict) else "")
        if due is not None and due.day <= ctx.today:
            tasks.append({**task, "_due": due})
    return tasks


def _todoist_url(task: Dict[str, Any]) -> str:
    return str(task.get("url") or f"https://app.todoist.com/app/task/{task['id']}")


def _todoist_row(ctx: Ctx, task: Dict[str, Any]) -> Row:
    due: Due = task["_due"]
    why = _due_why(ctx, due, "Due")
    item = _item(f"todoist:{task['id']}", str(task.get("content") or "Todoist task"), why, "todoist",
                 due=_iso_date(due), actions=["start", "defer", "wait", "done", "open"], open_url=_todoist_url(task))
    return Row(item, TIER_OVERDUE if due.day < ctx.today else TIER_DUE, due.at,
               _todoist_priority(task.get("priority")), today=True)


def _iso_date(due: Due) -> str:
    return due.day.isoformat() if due.all_day else _iso_z(due.at)


def _read_calendar(ctx: Ctx) -> List[Dict[str, Any]]:
    """Events on the person's calendars that touch today, recurring ones expanded."""
    from sqlalchemy import and_, or_

    from core import database as cdb
    from core.database import CalendarCal, CalendarEvent
    from routes.calendar_routes import _expand_rrule

    start = (ctx.day_start(ctx.today) - timedelta(days=1)).replace(tzinfo=None)
    end = (ctx.day_end + timedelta(days=1)).replace(tzinfo=None)
    db = cdb.SessionLocal()
    try:
        rows = (db.query(CalendarEvent).join(CalendarCal, CalendarCal.id == CalendarEvent.calendar_id)
                .filter(CalendarCal.owner == ctx.owner, CalendarEvent.status != "cancelled",
                        or_(and_(or_(CalendarEvent.rrule == "", CalendarEvent.rrule.is_(None)),
                                 CalendarEvent.dtstart < end, CalendarEvent.dtend > start),
                            and_(CalendarEvent.rrule.isnot(None), CalendarEvent.rrule != "",
                                 CalendarEvent.dtstart < end)))
                .order_by(CalendarEvent.dtstart).all())
        events: List[Dict[str, Any]] = []
        for row in rows:
            # The Todoist mirror calendar holds copies of tasks Today reads live.
            if row.origin == "todoist" or row.event_type == "todoist":
                continue
            events.extend(_expand_rrule(row, start, end))
        return events
    finally:
        db.close()


def _event_row(ctx: Ctx, ev: Dict[str, Any]) -> Optional[Row]:
    if ev.get("all_day"):
        first = date.fromisoformat(str(ev["dtstart"])[:10])
        last = date.fromisoformat(str(ev["dtend"])[:10]) - timedelta(days=1)
        if not first <= ctx.today <= max(first, last):
            return None
        due = _due_from_date(ctx, ctx.today)
        why = "On your calendar all day"
    else:
        due = _parse_due(ctx, ev["dtstart"])
        end = _parse_due(ctx, ev["dtend"])
        if due is None or not (due.at < ctx.day_end and (end.at if end else due.at) >= ctx.day_start(ctx.today)):
            return None
        if due.day != ctx.today:
            return None
        why = f"On your calendar at {_clock(due.at.astimezone(ctx.tz))}"
    item = _item(f"event:{ev['uid']}", str(ev.get("summary") or "Event"), why, "calendar", due=_iso_date(due),
                 actions=["open"], open_url="/calendar")
    return Row(item, TIER_DUE, due.at, today=True)


def _read_notes(ctx: Ctx) -> List[Any]:
    from src.notes_store import STORE

    notes = STORE.list(ctx.notes_owner, archived=False, allow_private=True)
    if ctx.notes_owner is None:
        return [n for n in notes if n.due_date]
    # The store treats a missing owner as "everyone" and keeps legacy rows; here a note is mine or nobody's.
    return [n for n in notes if n.due_date and n.owner == ctx.notes_owner]


def _note_row(ctx: Ctx, note: Any) -> Optional[Row]:
    due = _parse_due(ctx, note.due_date)
    if due is None or due.day > ctx.today:
        return None
    kind = "Checklist" if note.items is not None else "Reminder"
    if due.day < ctx.today:
        why = f"{kind} overdue since {_day_label(ctx, due.day)}"
    elif due.all_day:
        why = f"{kind} due today"
    else:
        why = f"{kind} due today at {_clock(due.at.astimezone(ctx.tz))}"
    title = (note.title or "").strip() or (note.content or "").strip().split("\n")[0][:80] or "Untitled note"
    actions = ["start", "defer", "wait", "open"] + ([] if (note.repeat or "none") != "none" else ["done"])
    item = _item(f"note:{note.id}", title, why, "note", due=_iso_date(due), actions=actions,
                 open_url=f"/notes?id={note.id}")
    return Row(item, TIER_OVERDUE if due.day < ctx.today else TIER_DUE, due.at,
               touched=_epoch(note.updated_at), today=True)


def _read_outcomes(ctx: Ctx) -> List[Dict[str, Any]]:
    return store.list_for(ctx.owner, _OPEN_STATES)


def _read_runs(ctx: Ctx) -> List[Dict[str, Any]]:
    from src import agent_activity

    # list_runs also admits rows with no owner; those are nobody's, so filter again.
    runs = [r for r in agent_activity.list_runs(owner=ctx.owner, limit=200) if r.get("owner") == ctx.owner]
    latest: Dict[str, float] = {}
    for run in runs:
        sid = _worker_session(run)
        if sid:
            latest[sid] = max(latest.get(sid, 0.0), float(run.get("started_at") or 0))
    out = []
    for run in runs:
        sid, finished = _worker_session(run), run.get("finished_at")
        if run.get("status") not in RESUMABLE_RUN_STATUSES or not sid or not finished:
            continue
        if ctx.now.timestamp() - float(finished) > RUN_WINDOW.total_seconds():
            continue
        # A later run in the same chat means someone already picked this one up.
        if latest.get(sid, 0.0) > float(run.get("started_at") or 0):
            continue
        out.append(run)
    return out


def _worker_session(run: Dict[str, Any]) -> str:
    return str((run.get("summary") or {}).get("target_session") or "")


def _run_row(ctx: Ctx, run: Dict[str, Any]) -> Row:
    finished = datetime.fromtimestamp(float(run["finished_at"]), timezone.utc)
    verb = "Interrupted" if run.get("status") == "interrupted" else "Stopped part way"
    title = str(run.get("title") or "Worker run")
    for prefix in ("Worker · ", "Sub-agent · "):
        title = title[len(prefix):] if title.startswith(prefix) else title
    sid = _worker_session(run)
    item = _item(f"run:{run['run_id']}", title, f"{verb} {_ago(ctx, finished)}", "agent_run",
                 state=run.get("status"), actions=["resume", "defer", "open"], open_url=_chat_url(sid))
    return Row(item, TIER_REST, group=GROUP_RUN, touched=finished.timestamp())


# ── outcomes ─────────────────────────────────────────────────────────────────

def _chat_url(session_id: str) -> Optional[str]:
    return f"/?session={session_id}" if session_id else None


def _link_ref(o: Dict[str, Any], kind: str) -> Optional[str]:
    refs = [link["ref"] for link in o.get("links", []) if link["kind"] == kind]
    return refs[-1] if refs else None


def _outcome_open_url(o: Dict[str, Any], todoist: Optional[Dict[str, Any]]) -> Optional[str]:
    chat = _link_ref(o, "chat")
    if chat:
        return _chat_url(chat)
    if todoist is not None:
        return _todoist_url(todoist)
    task_ref = _link_ref(o, "todoist_task")
    if task_ref:
        from urllib.parse import quote

        return f"https://app.todoist.com/app/task/{quote(task_ref, safe='')}"
    for kind, build in (("note", lambda r: f"/notes?id={r}"), ("calendar_event", lambda r: "/calendar"),
                        ("pull_request", str), ("url", str)):
        ref = _link_ref(o, kind)
        if ref:
            return build(ref)
    return None


def _outcome_actions(o: Dict[str, Any]) -> List[str]:
    has_chat = bool(_link_ref(o, "chat"))
    state = o["state"]
    actions = ["defer", "wait", "done"]
    if state == "open":
        actions.append("start")
    elif has_chat:
        actions.append("resume")
    elif state != "blocked":
        actions.append("start")
    return actions + ["open"]


def _outcome_row(ctx: Ctx, o: Dict[str, Any], live: Dict[str, Dict[str, Any]]) -> Optional[Row]:
    snoozed = o.get("snoozed_until")
    if snoozed and _utc(datetime.fromisoformat(snoozed)) > ctx.now:
        return None
    task = next((live[l["ref"]] for l in o["links"] if l["kind"] == "todoist_task" and l["ref"] in live), None)
    due = task["_due"] if task else _stored_due(ctx, _naive(o.get("due_at")))
    blocker = o.get("blocker")
    state = o["state"]
    priority = o.get("priority") or 5
    touched = _epoch(_naive(o.get("updated_at")))
    open_url = _outcome_open_url(o, task)

    def make(why: str, tier: int, *, today: bool, group: int = GROUP_OPEN, when: Optional[datetime] = None,
             shown_due: Optional[str] = None) -> Row:
        item = _item(f"outcome:{o['id']}", o["title"], why, "outcome", due=shown_due, state=state, blocker=blocker,
                     actions=_outcome_actions(o), open_url=open_url)
        return Row(item, tier, when, priority, group, touched, today)

    if state == "waiting":
        follow = _naive(o.get("follow_up_at"))
        if follow is None or _utc(follow) >= ctx.day_end:
            return None
        who = o.get("waiting_on") or "someone"
        when = "due today" if _utc(follow) >= ctx.day_start(ctx.today) else \
            f"was due {_day_label(ctx, _utc(follow).astimezone(ctx.tz).date())}"
        return make(f"Waiting on {who}, follow-up {when}", TIER_FOLLOW_UP, today=True, when=_utc(follow),
                    shown_due=_iso_z(follow))
    if due is not None and due.day <= ctx.today:
        tier = TIER_OVERDUE if due.day < ctx.today else TIER_DUE
        return make(_due_why(ctx, due, "Due"), tier, today=True, when=due.at, shown_due=_iso_date(due))
    shown = _iso_date(due) if due else None
    if state == "blocked":
        recovery = (blocker or {}).get("recovery")
        if recovery in ("user", "approval"):
            why = "Blocked, waiting on you" if recovery == "user" else "Blocked, waiting for your approval"
            return make(why, TIER_BLOCKED, today=True, when=_utc(_naive(blocker.get("at")) or ctx.now),
                        shown_due=shown)
        if recovery == "retry":
            return make("Blocked, safe to retry", TIER_REST, today=False, group=GROUP_ACTIVE, shown_due=shown)
        return None
    if state == "active":
        if (o.get("assignee") or "").startswith("agent:"):
            return None
        step = (o.get("next_action") or "").strip()
        why = f"In progress. Next step: {step[:120]}" if step else "In progress"
        return make(why, TIER_REST, today=False, group=GROUP_ACTIVE, shown_due=shown)
    if state == "open":
        why = f"Not started, priority {o['priority']}" if o.get("priority") else "Not started"
        return make(why, TIER_REST, today=False, shown_due=shown)
    return None


def _naive(value: Any) -> Optional[datetime]:
    """Outcome timestamps arrive as naive-UTC ISO strings."""
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    return _utc(parsed).replace(tzinfo=None)


# ── the page ─────────────────────────────────────────────────────────────────

async def _guarded(name: str, degraded: List[str], work: Awaitable, timeout: float,
                   absent: Optional[List[str]] = None) -> Any:
    try:
        return await asyncio.wait_for(work, timeout)
    except _Absent:
        if absent is not None:
            absent.append(name)
        return None
    except Exception as exc:  # noqa: BLE001 - one dead source must not take the page down
        logger.warning("today: source %s failed: %s", name, exc or type(exc).__name__)
        degraded.append(name)
        return None


def _thread(fn: Callable, *args) -> Awaitable:
    return asyncio.to_thread(fn, *args)


async def build(ctx: Ctx) -> Dict[str, Any]:
    degraded: List[str] = []
    absent: List[str] = []
    todoist, events, notes, outcomes, runs = await asyncio.gather(
        _guarded("todoist", degraded, _fetch_todoist(ctx), TODOIST_TIMEOUT + 0.5, absent),
        _guarded("calendar", degraded, _thread(_read_calendar, ctx), SOURCE_TIMEOUT),
        _guarded("notes", degraded, _thread(_read_notes, ctx), SOURCE_TIMEOUT),
        _guarded("outcomes", degraded, _thread(_read_outcomes, ctx), SOURCE_TIMEOUT),
        _guarded("agent_runs", degraded, _thread(_read_runs, ctx), SOURCE_TIMEOUT),
    )
    todoist, events, notes, outcomes, runs = (x or [] for x in (todoist, events, notes, outcomes, runs))

    # Anything an open outcome links to is shown as the outcome, snoozed or not.
    linked: Dict[str, set] = {}
    for o in outcomes:
        for link in o["links"]:
            linked.setdefault(link["kind"], set()).add(link["ref"])
    live = {str(t["id"]): t for t in todoist}

    rows: List[Row] = []
    for o in outcomes:
        try:
            row = _outcome_row(ctx, o, live)
        except Exception:  # noqa: BLE001 - a malformed row hides itself, not the page
            logger.warning("today: outcome %s skipped", o.get("id"), exc_info=True)
            row = None
        if row:
            rows.append(row)
    rows += [_todoist_row(ctx, t) for t in todoist if str(t["id"]) not in linked.get("todoist_task", ())]
    rows += [r for ev in events if ev["uid"].split("::")[0] not in linked.get("calendar_event", ())
             and (r := _event_row(ctx, ev))]
    rows += [r for n in notes if n.id not in linked.get("note", ()) and (r := _note_row(ctx, n))]
    rows += [_run_row(ctx, run) for run in runs
             if run["run_id"] not in linked.get("agent_run", ()) and _worker_session(run) not in linked.get("chat", ())]

    today_rows = sorted((r for r in rows if r.today), key=Row.sort_key)
    rest = sorted((r for r in rows if not r.today), key=Row.sort_key)
    shown, spill = today_rows[:TODAY_CAP], today_rows[TODAY_CAP:]
    nxt = (spill + rest)[:NEXT_CAP]
    return {"today": [r.item for r in shown], "next": [r.item for r in nxt], "degraded": degraded,
            "scope": {"today_limit": TODAY_CAP, "next_limit": NEXT_CAP,
                      "remaining": max(0, len(rows) - len(shown) - len(nxt))},
            "sources": {"todoist": "restricted" if not ctx.todoist else
                        "unavailable" if "todoist" in degraded else
                        "not_configured" if "todoist" in absent else "ready"},
            "generated_at": _iso_z(ctx.now)}


# ── actions ──────────────────────────────────────────────────────────────────

@dataclass
class Target:
    """The item an action was aimed at, resolved for this owner."""
    kind: str
    ref: str
    title: str = ""
    outcome: Optional[Dict[str, Any]] = None
    actions: List[str] = field(default_factory=list)
    open_url: Optional[str] = None
    worker: str = ""  # a worker chat that can be resumed
    link: Optional[Tuple[str, str]] = None  # (link kind, ref) used when an outcome is made for it
    extra_links: List[Tuple[str, str]] = field(default_factory=list)


async def _resolve(ctx: Ctx, key: str) -> Target:
    kind, _, ref = str(key or "").partition(":")
    if not ref:
        raise ActionError(404, "Item not found.")
    if kind == "outcome":
        o = await asyncio.to_thread(store.get, ctx.owner, ref)
        if o is None or o["state"] not in _OPEN_STATES:
            raise ActionError(404, "Item not found.")
        worker = (o.get("assignee") or "")[6:] if (o.get("assignee") or "").startswith("agent:") else ""
        task = None
        return Target("outcome", ref, o["title"], o, _outcome_actions(o), _outcome_open_url(o, task), worker)
    if kind == "todoist":
        try:
            tasks = await asyncio.wait_for(_fetch_todoist(ctx), TODOIST_TIMEOUT + 0.5)
        except (_Absent, asyncio.TimeoutError, RuntimeError):
            raise ActionError(404, "Item not found.")
        task = next((t for t in tasks if str(t["id"]) == ref), None)
        if task is None:
            raise ActionError(404, "That task is no longer on Today.")
        return Target("todoist", ref, str(task.get("content") or "Todoist task"), None,
                      ["start", "defer", "wait", "done", "open"], _todoist_url(task), link=("todoist_task", ref))
    if kind == "note":
        notes = await asyncio.to_thread(_read_notes_all, ctx)
        note = next((n for n in notes if n.id == ref), None)
        if note is None:
            raise ActionError(404, "Item not found.")
        actions = ["start", "defer", "wait", "open"] + ([] if (note.repeat or "none") != "none" else ["done"])
        return Target("note", ref, (note.title or "").strip() or "Untitled note", None, actions,
                      f"/notes?id={ref}", link=("note", ref))
    if kind == "event":
        if not await asyncio.to_thread(_owns_event, ctx, ref.split("::")[0]):
            raise ActionError(404, "Item not found.")
        return Target("event", ref, "", None, ["open"], "/calendar")
    if kind == "run":
        from src import agent_activity

        run = await asyncio.to_thread(agent_activity.get_run, ref)
        if not run or run.get("owner") != ctx.owner:
            raise ActionError(404, "Item not found.")
        sid = _worker_session(run)
        title = str(run.get("title") or "Worker run")
        return Target("run", ref, title, None, ["resume", "defer", "open"], _chat_url(sid), sid,
                      link=("agent_run", ref), extra_links=[("chat", sid)] if sid else [])
    raise ActionError(404, "Item not found.")


def _read_notes_all(ctx: Ctx) -> List[Any]:
    """Every live note of the owner, due or not: an action may name any of them."""
    from src.notes_store import STORE

    notes = STORE.list(ctx.notes_owner, archived=False, allow_private=True)
    return notes if ctx.notes_owner is None else [n for n in notes if n.owner == ctx.notes_owner]


def _owns_event(ctx: Ctx, uid: str) -> bool:
    from core import database as cdb
    from core.database import CalendarCal, CalendarEvent

    db = cdb.SessionLocal()
    try:
        return db.query(CalendarEvent.uid).join(CalendarCal, CalendarCal.id == CalendarEvent.calendar_id).filter(
            CalendarEvent.uid == uid, CalendarCal.owner == ctx.owner).first() is not None
    finally:
        db.close()


def _ok(result: Dict[str, Any]) -> Dict[str, Any]:
    if not result.get("ok"):
        status = {"not_found": 404, "needs_evidence": 409, "forbidden": 403}.get(result.get("code"), 400)
        raise ActionError(status, result.get("error") or "Request failed.")
    return result


async def _existing_outcome(ctx: Ctx, target: Target) -> Optional[Dict[str, Any]]:
    if target.outcome is not None:
        return target.outcome
    kind, ref = target.link or ("", "")
    found = [o for o in await asyncio.to_thread(store.find_by_link, ctx.owner, kind, ref)
             if o["state"] in _OPEN_STATES]
    return found[0] if found else None


async def _outcome_for(ctx: Ctx, target: Target, *, chat: Optional[str] = None) -> Dict[str, Any]:
    """The outcome behind the item, made and linked to it if there is none yet."""
    existing = await _existing_outcome(ctx, target)
    if existing:
        return existing
    kind, ref = target.link
    links = [{"kind": kind, "ref": ref, "label": target.title[:300]}]
    links += [{"kind": k, "ref": r} for k, r in target.extra_links]
    if chat:
        links.append({"kind": "chat", "ref": chat, "label": target.title[:300]})
    created = await asyncio.to_thread(
        lambda: store.create(ctx.owner, target.title or "Untitled", links=links, source="person", actor="person"))
    return _ok(created)["outcome"]


def _parse_until(ctx: Ctx, raw: Optional[str]) -> Optional[datetime]:
    """A date (9:00 that day) or a time, read in the person's zone unless it says Z."""
    if not raw:
        return None
    due = _parse_due(ctx, raw)
    if due is None:
        raise ActionError(400, "until must be a date or a date and time.")
    return ctx.at_hour(due.day) if due.all_day else due.at


def _naive_utc(dt: datetime) -> datetime:
    return _utc(dt).replace(tzinfo=None)


async def perform(ctx: Ctx, key: str, action: str, *, until: Optional[str] = None,
                  waiting_on: Optional[str] = None) -> Dict[str, Any]:
    if action not in ACTIONS:
        raise ActionError(400, f"action must be one of {', '.join(ACTIONS)}.")
    target = await _resolve(ctx, key)
    if action not in target.actions:
        raise ActionError(400, f"{action} is not available for this item.")
    when = _parse_until(ctx, until)

    if action == "open":
        return {"ok": True, "open_url": target.open_url}
    if action == "defer":
        snooze = when or ctx.at_hour(ctx.today + timedelta(days=1))
        if snooze <= ctx.now:
            raise ActionError(400, "Pick a time in the future.")
        o = await _outcome_for(ctx, target)
        _ok(await asyncio.to_thread(store.update, ctx.owner, o["id"], {"snoozed_until": _naive_utc(snooze)}, "person"))
        return {"ok": True, "outcome_id": o["id"], "snoozed_until": _iso_z(snooze)}
    if action == "wait":
        who = (waiting_on or "").strip()
        if not who:
            raise ActionError(400, "Say who or what you are waiting on.")
        follow = when or ctx.at_hour(ctx.today + timedelta(days=WAIT_DAYS))
        o = await _outcome_for(ctx, target)
        _ok(await asyncio.to_thread(lambda: store.set_state(
            ctx.owner, o["id"], "waiting", "person", waiting_on=who, follow_up_at=_naive_utc(follow))))
        return {"ok": True, "outcome_id": o["id"], "follow_up_at": _iso_z(follow)}
    if action == "done":
        return await _done(ctx, target)
    if action == "start":
        return await _start(ctx, target)
    return await _resume(ctx, target)


async def _done(ctx: Ctx, target: Target) -> Dict[str, Any]:
    if target.kind == "outcome":
        _ok(await asyncio.to_thread(lambda: store.set_state(
            ctx.owner, target.ref, "done", "person", confirmed_by_person=True)))
        return {"ok": True, "outcome_id": target.ref}
    if target.kind == "todoist":
        from src import todoist_calendar_sync as td

        code, stdout, stderr = await td._run_td(["task", "complete", target.ref], timeout=int(TODOIST_TIMEOUT))
        if code != 0:
            raise ActionError(502, f"Todoist did not complete the task: {stderr or stdout or 'td failed'}")
    elif target.kind == "note":
        await asyncio.to_thread(_archive_note, ctx, target.ref)
    else:
        raise ActionError(400, "done is not available for this item.")
    # The item is closed in its own system; tell any outcome that points at it.
    for o in await asyncio.to_thread(store.find_by_link, ctx.owner, *(target.link or ("", ""))):
        if o["state"] in _OPEN_STATES:
            await asyncio.to_thread(store.add_evidence, ctx.owner, o["id"], {
                "kind": "note", "ref": f"{target.kind}:{target.ref}",
                "detail": f"Marked done from Today: {target.title}"[:300]}, "person")
    return {"ok": True}


def _archive_note(ctx: Ctx, note_id: str) -> None:
    from src.notes_store import STORE

    note = next((n for n in _read_notes_all(ctx) if n.id == note_id), None)
    if note is None:
        raise ActionError(404, "Item not found.")
    note.archived = True
    STORE.save(note, enforce_readonly=False)  # a person's own action; only agent calls are held to read-only


def _default_chat_endpoint(owner: str) -> Optional[Tuple[str, str, Dict]]:
    """The person's default chat model as (chat url, model, headers), or None when none is set."""
    from src.endpoint_resolver import resolve_endpoint

    url, model, headers = resolve_endpoint("default", owner=owner)
    return (url, model, headers or {}) if url and model else None


def _seed_text(o: Dict[str, Any]) -> str:
    lines = [f"Working on: {o['title']}"]
    if o.get("detail"):
        lines.append(o["detail"].strip()[:600])
    if o.get("next_action"):
        lines.append(f"Next step: {o['next_action']}")
    if o.get("waiting_on"):
        lines.append(f"Waiting on: {o['waiting_on']}")
    lines.append("What should we do first?")
    return "\n\n".join(lines)


def _create_chat(owner: str, o: Dict[str, Any]) -> str:
    """A new chat for the outcome, opened with its title, detail and next step."""
    from core.models import ChatMessage
    from src.ai_interaction import get_session_manager

    manager = get_session_manager()
    endpoint = _default_chat_endpoint(owner)
    if manager is None or endpoint is None:
        raise ActionError(409, "Choose a default chat model in Settings, then start again.")
    url, model, headers = endpoint
    sid = str(uuid.uuid4())
    sess = manager.create_session(session_id=sid, name=o["title"][:60], endpoint_url=url, model=model, rag=False,
                                  owner=owner)
    if headers and hasattr(sess, "headers"):
        sess.headers = headers
    sess.add_message(ChatMessage("assistant", _seed_text(o), {"source": "today", "outcome_id": o["id"]}))
    manager.save_sessions()
    return sid


async def _start(ctx: Ctx, target: Target) -> Dict[str, Any]:
    o = await _existing_outcome(ctx, target)
    chat = _link_ref(o, "chat") if o else None
    if not chat:
        # Make the chat first: with no default model, nothing else should change.
        chat = await asyncio.to_thread(_create_chat, ctx.owner, o or {"id": "", "title": target.title})
    o = await _outcome_for(ctx, target, chat=chat)
    if not _link_ref(o, "chat"):
        _ok(await asyncio.to_thread(lambda: store.add_link(ctx.owner, o["id"], "chat", chat, label=o["title"][:300])))
    if o["state"] != "active":
        _ok(await asyncio.to_thread(store.set_state, ctx.owner, o["id"], "active", "person"))
    return {"ok": True, "outcome_id": o["id"], "open_url": _chat_url(chat)}


async def _resume_worker(owner: str, session_id: str) -> Dict[str, Any]:
    """Send a worker chat back to work through the same path as send_to_session.

    Same chat, saved workspace, preflight, capacity and approval rules.
    """
    from src.agent_tools.session_tools import send_to_session

    message = ("[Today] The person asked you to continue. Pick the task up from where it stopped; "
               "do not redo finished steps, and end with the usual Outcome / Changed / Checked / Open report.")
    content = json.dumps({"session_id": session_id, "message": message, "mode": "agent"})
    return await send_to_session(content, None, owner)


def _watch(task: "asyncio.Future") -> None:
    _BACKGROUND.add(task)

    def done(finished: "asyncio.Future") -> None:
        _BACKGROUND.discard(finished)
        if not finished.cancelled() and finished.exception():
            logger.warning("today: resumed worker failed: %s", finished.exception())

    task.add_done_callback(done)


async def _resume(ctx: Ctx, target: Target) -> Dict[str, Any]:
    chat = target.worker or (_link_ref(target.outcome, "chat") if target.outcome else "") or ""
    open_url = _chat_url(chat) or target.open_url
    if not target.worker:
        return {"ok": True, "open_url": open_url, "resumed": False}
    task = asyncio.ensure_future(_resume_worker(ctx.owner, target.worker))
    _watch(task)
    done, _ = await asyncio.wait({task}, timeout=RESUME_WAIT)
    result = task.result() if done and not task.exception() else None
    if done and task.exception():
        return {"ok": False, "error": f"The worker could not be resumed: {task.exception()}", "open_url": open_url}
    if isinstance(result, dict) and result.get("error"):
        blocker = None
        if result.get("code") or result.get("problems"):
            blocker = blockers.from_preflight(result).to_dict()
            if target.outcome is not None:
                await asyncio.to_thread(store.set_blocker, ctx.owner, target.outcome["id"], blocker, "person")
        return {"ok": False, "error": result["error"], "blocker": blocker, "open_url": open_url}
    if target.outcome is not None and target.outcome["state"] == "blocked":
        await asyncio.to_thread(store.set_blocker, ctx.owner, target.outcome["id"], None, "person")
    return {"ok": True, "open_url": open_url, "resumed": True, "running": not done}
