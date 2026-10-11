"""Outcomes: one record per intent the person wants closed.

An outcome links to the systems of record (Todoist, calendar, notes, chats,
runs, branches, pull requests) and never copies their title, due date or
done-state. Three rules hold everywhere in this module:

* **Owner scope.** Every function takes the owner and filters on it. An empty
  owner reads and writes nothing; another owner's id looks like a missing row.
* **Done needs evidence.** `set_state(..., "done")` succeeds only with at least
  one evidence item, or with the person's own confirmation. Only the actor
  `person` can confirm, whatever the caller passes.
* **Nothing silent.** An agent actor cannot set a priority or change a due
  date that is already set. Every successful mutation appends an event.

Functions open their own session and return result dicts: `{"ok": True, ...}`
or `{"ok": False, "code": ..., "error": ...}`. They do not raise into callers.
"""

from __future__ import annotations

import functools
import itertools
import json
import logging
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from core import database as cdb
from core.database import Outcome, OutcomeEvent, OutcomeLink
from src.blockers import Blocker

logger = logging.getLogger(__name__)

STATES = ("open", "active", "waiting", "blocked", "done", "dropped")
CLOSED_STATES = ("done", "dropped")
KINDS = ("project", "obligation")
SOURCES = ("person", "agent", "capture")
LINK_KINDS = ("todoist_task", "calendar_event", "note", "chat", "agent_run", "scheduled_task",
              "worktree", "branch", "pull_request", "url")
EVIDENCE_KINDS = ("commit", "pull_request", "run", "tests", "review", "note")
EDITABLE = ("title", "detail", "next_action", "waiting_on", "follow_up_at", "due_at", "priority",
            "snoozed_until", "assignee", "kind")
_DATE_FIELDS = ("follow_up_at", "due_at", "snoozed_until")
_TEXT_FIELDS = ("detail", "next_action", "waiting_on", "assignee")
_MAX_EVIDENCE = 100
_EVENT_LIMIT = 50
CONFIRM_NEXT_ACTION = "Confirm the result or ask for evidence"

_event_seq = itertools.count()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _event_id() -> str:
    # Sorts by creation order, so history reads in order even when two events share a timestamp.
    return f"{time.time_ns():020d}{next(_event_seq):06d}{uuid.uuid4().hex[:8]}"


def _err(code: str, error: str) -> Dict[str, Any]:
    return {"ok": False, "code": code, "error": error}


def _is_agent(actor: str) -> bool:
    return str(actor or "").startswith("agent:")


@contextmanager
def _session():
    db = cdb.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _guarded(fn):
    """Turn an unexpected failure into an error result instead of raising."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - callers include the agent loop
            logger.warning("outcomes.%s failed: %s", fn.__name__, exc, exc_info=True)
            return _err("error", f"{fn.__name__} failed: {exc}")
    return wrapper


def _parse_dt(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _load_json(raw: Optional[str], default: Any) -> Any:
    try:
        value = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) or default is None else default


def _link_dict(link: OutcomeLink) -> Dict[str, Any]:
    return {"id": link.id, "kind": link.kind, "ref": link.ref, "label": link.label,
            "created_at": _iso(link.created_at)}


def _event_dict(ev: OutcomeEvent) -> Dict[str, Any]:
    return {"id": ev.id, "at": _iso(ev.at), "actor": ev.actor, "kind": ev.kind,
            "detail": _load_json(ev.detail_json, {})}


def _outcome_dict(o: Outcome, links: Iterable[OutcomeLink] = ()) -> Dict[str, Any]:
    return {
        "id": o.id, "owner": o.owner, "title": o.title, "detail": o.detail, "kind": o.kind,
        "state": o.state, "next_action": o.next_action, "assignee": o.assignee,
        "waiting_on": o.waiting_on, "follow_up_at": _iso(o.follow_up_at), "due_at": _iso(o.due_at),
        "priority": o.priority, "snoozed_until": _iso(o.snoozed_until),
        "blocker": _load_json(o.blocker_json, None) if o.blocker_json else None,
        "evidence": _load_json(o.evidence_json, []),
        "confirmed_by_person": bool(o.confirmed_by_person), "completed_at": _iso(o.completed_at),
        "source": o.source, "created_at": _iso(o.created_at), "updated_at": _iso(o.updated_at),
        "links": [_link_dict(link) for link in links],
    }


def _load(db, owner: Optional[str], outcome_id: str) -> Optional[Outcome]:
    if not owner or not outcome_id:
        return None
    return db.query(Outcome).filter(Outcome.id == str(outcome_id), Outcome.owner == owner).first()


def _links_of(db, outcome_id: str) -> List[OutcomeLink]:
    return db.query(OutcomeLink).filter(OutcomeLink.outcome_id == outcome_id).order_by(OutcomeLink.created_at).all()


def _snapshot(db, o: Outcome) -> Dict[str, Any]:
    return _outcome_dict(o, _links_of(db, o.id))


def _event(db, outcome_id: str, actor: str, kind: str, detail: Optional[Dict[str, Any]] = None) -> None:
    db.add(OutcomeEvent(id=_event_id(), outcome_id=outcome_id, at=_now(), actor=actor or "system",
                        kind=kind, detail_json=json.dumps(detail or {}, default=str)))


def _clip(value: Any, limit: int = 200) -> Any:
    return value[:limit] if isinstance(value, str) else value


def _agent_limits(actor: str, current: Optional[Outcome], fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """An agent never sets a priority and never replaces a due date the person set."""
    if not _is_agent(actor):
        return None
    if fields.get("priority") is not None:
        return _err("forbidden", "Only the person sets a priority.")
    if fields.get("due_at") not in (None, "") and current is not None and current.due_at is not None:
        return _err("forbidden", "Only the person changes a due date that is already set.")
    return None


def _normalise(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Validated column values for the given editable fields."""
    out: Dict[str, Any] = {}
    for name, value in fields.items():
        if name == "title":
            title = str(value or "").strip()[:300]
            if not title:
                raise ValueError("title cannot be empty")
            out[name] = title
        elif name == "kind":
            if value not in KINDS:
                raise ValueError(f"kind must be one of {KINDS}")
            out[name] = value
        elif name == "priority":
            if value is None or value == "":
                out[name] = None
            else:
                prio = int(value)
                if not 1 <= prio <= 4:
                    raise ValueError("priority must be between 1 and 4")
                out[name] = prio
        elif name in _DATE_FIELDS:
            out[name] = _parse_dt(value)
        elif name in _TEXT_FIELDS:
            text = None if value is None else str(value).strip()
            out[name] = text or None
    return out


# ── reads ────────────────────────────────────────────────────────────────────

def get(owner: Optional[str], outcome_id: str, *, events: int = _EVENT_LIMIT) -> Optional[Dict[str, Any]]:
    """The outcome with its links, evidence and most recent events, or None."""
    with _session() as db:
        o = _load(db, owner, outcome_id)
        if o is None:
            return None
        data = _snapshot(db, o)
        data["events"] = _history(db, o.id, events)
        return data


def list_for(owner: Optional[str], states: Optional[Iterable[str]] = None, *, limit: int = 500) -> List[Dict[str, Any]]:
    """The owner's outcomes, most recently touched first, each with its links."""
    if not owner:
        return []
    with _session() as db:
        q = db.query(Outcome).filter(Outcome.owner == owner)
        if states:
            q = q.filter(Outcome.state.in_(list(states)))
        rows = q.order_by(Outcome.updated_at.desc(), Outcome.id).limit(limit).all()
        by_outcome: Dict[str, List[OutcomeLink]] = {}
        if rows:
            ids = [o.id for o in rows]
            for link in db.query(OutcomeLink).filter(OutcomeLink.outcome_id.in_(ids)).order_by(OutcomeLink.created_at):
                by_outcome.setdefault(link.outcome_id, []).append(link)
        return [_outcome_dict(o, by_outcome.get(o.id, ())) for o in rows]


def find_by_link(owner: Optional[str], kind: str, ref: str) -> List[Dict[str, Any]]:
    """The owner's outcomes linked to this target, most recently touched first."""
    if not owner or not kind or not ref:
        return []
    with _session() as db:
        rows = (db.query(Outcome).join(OutcomeLink, OutcomeLink.outcome_id == Outcome.id)
                .filter(Outcome.owner == owner, OutcomeLink.kind == kind, OutcomeLink.ref == str(ref))
                .order_by(Outcome.updated_at.desc(), Outcome.id).all())
        return [_snapshot(db, o) for o in rows]


def _history(db, outcome_id: str, limit: int) -> List[Dict[str, Any]]:
    rows = (db.query(OutcomeEvent).filter(OutcomeEvent.outcome_id == outcome_id)
            .order_by(OutcomeEvent.id.desc()).limit(max(1, int(limit))).all())
    return [_event_dict(ev) for ev in reversed(rows)]


def history(owner: Optional[str], outcome_id: str, limit: int = _EVENT_LIMIT) -> List[Dict[str, Any]]:
    """The last `limit` events, oldest first. Empty for an outcome the owner does not have."""
    with _session() as db:
        o = _load(db, owner, outcome_id)
        return _history(db, o.id, limit) if o else []


# ── writes ───────────────────────────────────────────────────────────────────

@_guarded
def create(owner: Optional[str], title: str, *, kind: str = "obligation", detail: Optional[str] = None,
           next_action: Optional[str] = None, assignee: Optional[str] = None, waiting_on: Optional[str] = None,
           follow_up_at: Any = None, due_at: Any = None, priority: Any = None, snoozed_until: Any = None,
           state: str = "open", source: str = "person", links: Optional[List[Dict[str, Any]]] = None,
           actor: str = "person") -> Dict[str, Any]:
    if not owner:
        return _err("invalid", "An outcome needs an owner.")
    if source not in SOURCES:
        return _err("invalid", f"source must be one of {SOURCES}")
    if state not in ("open", "active", "waiting"):
        return _err("invalid", "A new outcome starts open, active or waiting.")
    try:
        values = _normalise({"title": title, "kind": kind, "detail": detail, "next_action": next_action,
                             "assignee": assignee, "waiting_on": waiting_on, "follow_up_at": follow_up_at,
                             "due_at": due_at, "priority": priority, "snoozed_until": snoozed_until})
    except (ValueError, TypeError) as exc:
        return _err("invalid", str(exc))
    if state == "waiting" and not values.get("waiting_on"):
        return _err("needs_waiting_on", "A waiting outcome needs waiting_on: who or what you are waiting for.")
    limited = _agent_limits(actor, None, {"priority": values.get("priority")})
    if limited:
        return limited
    checked_links = []
    for item in links or []:
        link_kind, ref = str((item or {}).get("kind") or ""), str((item or {}).get("ref") or "").strip()
        if link_kind not in LINK_KINDS or not ref:
            return _err("invalid", f"link kind must be one of {LINK_KINDS} and ref must not be empty")
        checked_links.append((link_kind, ref[:1000], _clip((item or {}).get("label"), 300)))
    with _session() as db:
        o = Outcome(id=uuid.uuid4().hex, owner=owner, state=state, source=source, evidence_json="[]",
                    confirmed_by_person=False, **values)
        db.add(o)
        db.flush()
        _event(db, o.id, actor, "created", {"source": source, "state": state})
        seen = set()
        for link_kind, ref, label in checked_links:
            if (link_kind, ref) in seen:
                continue
            seen.add((link_kind, ref))
            db.add(OutcomeLink(id=uuid.uuid4().hex, outcome_id=o.id, kind=link_kind, ref=ref, label=label,
                               created_at=_now()))
            _event(db, o.id, actor, "link", {"added": {"kind": link_kind, "ref": ref}})
        db.commit()
        return {"ok": True, "outcome": _snapshot(db, o)}


@_guarded
def update(owner: Optional[str], outcome_id: str, fields: Dict[str, Any], actor: str = "person") -> Dict[str, Any]:
    """Edit title, detail, next action, waiting/follow-up/due/snooze dates, priority and assignee.

    State, evidence, confirmation and ownership have their own paths; keys for
    them are ignored and listed under `ignored`.
    """
    fields = dict(fields or {})
    ignored = sorted(k for k in fields if k not in EDITABLE)
    wanted = {k: v for k, v in fields.items() if k in EDITABLE}
    with _session() as db:
        o = _load(db, owner, outcome_id)
        if o is None:
            return _err("not_found", "Outcome not found.")
        try:
            values = _normalise(wanted)
        except (ValueError, TypeError) as exc:
            return _err("invalid", str(exc))
        limited = _agent_limits(actor, o, values)
        if limited:
            return limited
        changed = {}
        for name, value in values.items():
            if getattr(o, name) != value:
                setattr(o, name, value)
                changed[name] = _iso(value) if isinstance(value, datetime) else _clip(value)
        if changed:
            _event(db, o.id, actor, "edit", {"fields": changed})
        db.commit()
        return {"ok": True, "outcome": _snapshot(db, o), "ignored": ignored}


def _move(db, o: Outcome, new_state: str, actor: str, **detail: Any) -> None:
    """Change state, keep the fields that depend on it consistent, and log it."""
    old = o.state
    if old == "waiting" and new_state != "waiting":
        o.waiting_on, o.follow_up_at = None, None
    if old == "done" and new_state != "done":
        o.confirmed_by_person = False
    if old in CLOSED_STATES and new_state not in CLOSED_STATES:
        o.completed_at = None
    if new_state in CLOSED_STATES:
        o.completed_at = _now()
    o.state = new_state
    _event(db, o.id, actor, "state", {"from": old, "to": new_state, **detail})


def _drop_blocker(db, o: Outcome, actor: str) -> None:
    previous = _load_json(o.blocker_json, None) if o.blocker_json else None
    o.blocker_json = None
    _event(db, o.id, actor, "blocker", {"cleared": True, "code": (previous or {}).get("code")})


@_guarded
def set_state(owner: Optional[str], outcome_id: str, state: str, actor: str = "person", *,
              confirmed_by_person: bool = False, waiting_on: Optional[str] = None,
              follow_up_at: Any = None) -> Dict[str, Any]:
    """Move an outcome to `state`, enforcing the done and waiting rules.

    `confirmed_by_person` counts only when `actor` is `person`.
    """
    if state not in STATES:
        return _err("invalid", f"state must be one of {STATES}")
    confirm = bool(confirmed_by_person) and actor == "person"
    with _session() as db:
        o = _load(db, owner, outcome_id)
        if o is None:
            return _err("not_found", "Outcome not found.")
        if state == "blocked" and not o.blocker_json:
            return _err("invalid", "An outcome becomes blocked by setting a blocker, not by choosing the state.")
        if state == "done" and not (_load_json(o.evidence_json, []) or o.confirmed_by_person or confirm):
            return _err("needs_evidence",
                        "Done needs at least one evidence item (a commit, pull request, run, test result or "
                        "review) or the person's own confirmation. Attach evidence, or ask the person to confirm.")
        if state == "waiting":
            who = (waiting_on if waiting_on is not None else o.waiting_on) or ""
            if not str(who).strip():
                return _err("needs_waiting_on", "A waiting outcome needs waiting_on: who or what you are waiting for.")
            try:
                follow_up = _parse_dt(follow_up_at) if follow_up_at is not None else None
            except ValueError as exc:
                return _err("invalid", str(exc))
            o.waiting_on = str(who).strip()
            if follow_up is not None:
                o.follow_up_at = follow_up
        changed = state != o.state
        if confirm and not o.confirmed_by_person:
            o.confirmed_by_person = True
            changed = True
        if changed:
            if state != o.state:
                if state not in ("blocked", "waiting") and o.blocker_json:
                    _drop_blocker(db, o, actor)
                _move(db, o, state, actor, confirmed=confirm)
            else:
                _event(db, o.id, actor, "state", {"from": o.state, "to": state, "confirmed": confirm})
        elif state == "waiting":
            _event(db, o.id, actor, "state", {"from": o.state, "to": state, "waiting_on": o.waiting_on})
        db.commit()
        return {"ok": True, "outcome": _snapshot(db, o)}


@_guarded
def set_blocker(owner: Optional[str], outcome_id: str, blocker: Any, actor: str = "system") -> Dict[str, Any]:
    """Set the one current blocker (a `Blocker` or its dict), or clear it with None.

    Setting moves an open or active outcome to blocked; clearing moves a
    blocked one back to active.
    """
    with _session() as db:
        o = _load(db, owner, outcome_id)
        if o is None:
            return _err("not_found", "Outcome not found.")
        if blocker is None:
            if o.blocker_json:
                _drop_blocker(db, o, actor)
            if o.state == "blocked":
                _move(db, o, "active", actor, reason="blocker cleared")
        else:
            if o.state in CLOSED_STATES:
                return _err("invalid", "A finished outcome cannot be blocked. Reopen it first.")
            item = blocker if isinstance(blocker, Blocker) else Blocker.from_dict(blocker)
            o.blocker_json = json.dumps(item.to_dict(), default=str)
            _event(db, o.id, actor, "blocker", {"set": True, "code": item.code, "recovery": item.recovery})
            if o.state in ("open", "active"):
                _move(db, o, "blocked", actor, reason=item.code)
        db.commit()
        return {"ok": True, "outcome": _snapshot(db, o)}


@_guarded
def add_link(owner: Optional[str], outcome_id: str, kind: str, ref: str, *, label: Optional[str] = None,
             actor: str = "person") -> Dict[str, Any]:
    """Link an outcome to an item in another system. Adding the same link again returns it."""
    ref = str(ref or "").strip()
    if kind not in LINK_KINDS:
        return _err("invalid", f"link kind must be one of {LINK_KINDS}")
    if not ref:
        return _err("invalid", "A link needs a ref.")
    with _session() as db:
        o = _load(db, owner, outcome_id)
        if o is None:
            return _err("not_found", "Outcome not found.")
        existing = (db.query(OutcomeLink).filter(OutcomeLink.outcome_id == o.id, OutcomeLink.kind == kind,
                                                 OutcomeLink.ref == ref[:1000]).first())
        if existing:
            return {"ok": True, "link": _link_dict(existing), "created": False}
        link = OutcomeLink(id=uuid.uuid4().hex, outcome_id=o.id, kind=kind, ref=ref[:1000],
                           label=_clip(label, 300) or None, created_at=_now())
        db.add(link)
        _event(db, o.id, actor, "link", {"added": {"kind": kind, "ref": ref[:200]}})
        o.updated_at = _now()
        db.commit()
        return {"ok": True, "link": _link_dict(link), "created": True}


@_guarded
def remove_link(owner: Optional[str], outcome_id: str, link_id: str, actor: str = "person") -> Dict[str, Any]:
    with _session() as db:
        o = _load(db, owner, outcome_id)
        link = (db.query(OutcomeLink).filter(OutcomeLink.id == str(link_id), OutcomeLink.outcome_id == o.id).first()
                if o else None)
        if o is None or link is None:
            return _err("not_found", "Link not found.")
        db.delete(link)
        _event(db, o.id, actor, "link", {"removed": {"kind": link.kind, "ref": link.ref[:200]}})
        o.updated_at = _now()
        db.commit()
        return {"ok": True}


@_guarded
def add_evidence(owner: Optional[str], outcome_id: str, item: Dict[str, Any], actor: str = "agent") -> Dict[str, Any]:
    """Attach one evidence item. `by` is always the acting caller, whatever the item says."""
    item = item if isinstance(item, dict) else {}
    kind, ref = str(item.get("kind") or ""), str(item.get("ref") or "").strip()
    if kind not in EVIDENCE_KINDS:
        return _err("invalid", f"evidence kind must be one of {EVIDENCE_KINDS}")
    if not ref:
        return _err("invalid", "Evidence needs a ref: a sha, URL or run id.")
    with _session() as db:
        o = _load(db, owner, outcome_id)
        if o is None:
            return _err("not_found", "Outcome not found.")
        items = _load_json(o.evidence_json, [])
        if any(e.get("kind") == kind and e.get("ref") == ref[:500] for e in items):
            return {"ok": True, "created": False, "outcome": _snapshot(db, o)}
        entry = {"kind": kind, "ref": ref[:500], "detail": str(item.get("detail") or "")[:300],
                 "at": _now().isoformat(timespec="seconds"), "by": actor}
        o.evidence_json = json.dumps((items + [entry])[-_MAX_EVIDENCE:])
        _event(db, o.id, actor, "evidence", entry)
        db.commit()
        return {"ok": True, "created": True, "outcome": _snapshot(db, o)}


# ── worker hand-back ─────────────────────────────────────────────────────────

def record_worker_handback(owner: Optional[str], session_id: str, run_id: str, text: Optional[str],
                           run_status: Optional[str], run_summary: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Apply a worker's final report to the outcomes it serves.

    Outcomes are found through the worker chat, the run, and the parent chat
    the run was started from. Evidence is attached; the state follows the
    hand-back:

    * done with evidence: `done`. Done without evidence: stays `active` and asks
      for confirmation.
    * partly done: `active`, next action from the first `Open:` item.
    * blocked, or any `Needs user:` / `Needs parent:` line: a blocker is set.

    A waiting outcome only takes the evidence. Never raises: the caller is the
    agent-control hand-back path, which must deliver the report regardless.
    Returns what it did, for logging.
    """
    result: Dict[str, Any] = {"outcomes": [], "error": None}
    try:
        from src import blockers
        from src.handback import evidence, parse_handback

        summary = run_summary if isinstance(run_summary, dict) else {}
        actor = f"agent:{session_id}"
        found: Dict[str, Dict[str, Any]] = {}
        for kind, ref in (("chat", session_id), ("agent_run", run_id), ("chat", summary.get("parent_session"))):
            for o in find_by_link(owner, kind, str(ref or "")):
                if o["state"] not in CLOSED_STATES:
                    found.setdefault(o["id"], o)
        if not found:
            return result

        hb = parse_handback(text or "")
        items = evidence(hb, summary, run_id=str(run_id or ""), by=actor)
        source = {"kind": "chat", "ref": str(session_id or "")}
        needs = blockers.from_needs(text or "", source=source)
        if needs is None and hb.outcome == "blocked":
            needs = blockers.worker_reported_blocked(hb.outcome_text or (hb.open[0] if hb.open else ""), source=source)

        for oid, o in found.items():
            actions: List[str] = []
            for item in items:
                if add_evidence(owner, oid, item, actor).get("created"):
                    actions.append(f"evidence:{item['kind']}")
            if o["state"] != "waiting":
                if needs is not None:
                    if set_blocker(owner, oid, needs, actor).get("ok"):
                        actions.append(f"blocked:{needs.code}")
                elif hb.outcome == "done":
                    res = set_state(owner, oid, "done", actor)
                    if res.get("ok"):
                        actions.append("done")
                    elif res.get("code") == "needs_evidence":
                        set_state(owner, oid, "active", actor)
                        update(owner, oid, {"next_action": CONFIRM_NEXT_ACTION}, actor)
                        actions.append("awaiting_confirmation")
                elif hb.outcome == "partial":
                    set_state(owner, oid, "active", actor)
                    if hb.open:
                        update(owner, oid, {"next_action": hb.open[0]}, actor)
                    actions.append("active")
            now = get(owner, oid, events=0)
            result["outcomes"].append({"id": oid, "state": (now or o)["state"], "actions": actions})
    except Exception as exc:  # noqa: BLE001
        logger.warning("record_worker_handback failed: %s", exc, exc_info=True)
        result["error"] = str(exc)
    return result
