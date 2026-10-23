"""Agents dashboard API — one owner-scoped view of every agent the user runs.

Unlike ``/api/workbench`` (admin-only, repository-centric), everything here is
scoped to the caller's own chats, so any signed-in user can watch, steer,
stop and launch their agents.

* ``GET  /overview``            — every chat with agent activity: status,
  latest step, live children, pending approvals, plus totals.
* ``GET  /stream``              — SSE of the caller's activity events.
* ``GET  /runs/{id}/events``    — one run's events.
* ``POST /runs/{id}/stop``      — stop one run.
* ``POST /sessions/{id}/steer`` — queue a mid-task instruction for a running turn.
* ``GET  /sessions/{id}/steer`` — that chat's recent steering messages and the
  state each one reached (queued / acknowledged / injected / cancelled / failed).
* ``GET  /approvals``           — pending tool approvals across the caller's chats.
* ``POST /launch``              — start a worker profile in a fresh chat.
* ``GET  /profiles``            — the configured loadouts, for pickers.
* ``GET  /profiles/export``     — admin: the loadouts as a JSON download
  (``?names=a,b`` for some of them).
* ``POST /profiles/import``     — admin: store loadouts from such a file
  (merge or replace), validated exactly like a Settings save.
* ``GET  /profiles/templates``  — admin: loadout templates shipped by the
  integrations that are available here.
* ``POST /profiles/templates/install`` — admin: import one of them
  (``{"integration", "template", "overwrite"}``); ``{server:<name>}`` references
  resolve to this install's MCP server ids.
* ``POST /sessions/{id}/loadout`` — run an existing chat under a loadout, or
  (``profile: null``) return it to the default setup.
* ``POST /sessions/{id}/archive`` — safely hide an idle chat without deleting
  its transcript or run history (and restore it with ``/unarchive``).
"""

from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace
from typing import Any, Dict, Optional, Set

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from src import agent_activity as activity
from src import agent_control, agent_runs, open_needs, tool_approvals
from src.auth_helpers import effective_user
from src.shell_access import resolve as _shell_mode

logger = logging.getLogger(__name__)

_RECENT_S = 24 * 3600

_ACTIVITY_TO_RUN_STATUS = {"running": "running", "waiting_approval": "waiting_approval",
                           "completed": "done", "done": "done", "failed": "error",
                           "error": "error", "blocked": "blocked", "partial": "partial",
                           "cancelled": "stopped", "stopped": "stopped"}


def _run_from_activity(rec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A chat-run-shaped record for a chat known only from its activity record."""
    if not rec:
        return {}
    status = _ACTIVITY_TO_RUN_STATUS.get(str(rec.get("status") or ""))
    if not status:
        return {}
    return {"status": status, "started_at": rec.get("started_at"), "finished_at": rec.get("finished_at"),
            "source": rec.get("source"), "needs_user": list((rec.get("summary") or {}).get("needs_user") or [])}
_LIVE_SOURCES = {"session", "claude_code", "bg_job", "pipeline", "worktree"}
SOLDIER_APPEARANCES = frozenset({"primary", "worker", "scout", "reviewer", "specialist", "sword", "helmet"})


def _child_with_live_approval(child: Dict[str, Any], pending: Dict[str, int]) -> Dict[str, Any]:
    """Re-derive a child run's `waiting_approval` from the live approval store.

    ``pending`` maps chat id to its count of unexpired approvals. A child whose
    chat holds none is reported as ``approval_expired``: the run is still
    paused, but there is no card left to answer.
    """
    if child.get("status") != "waiting_approval":
        return child
    target = str((child.get("summary") or {}).get("target_session") or child.get("session_id") or "")
    if target and pending.get(target):
        return child
    return {**child, "status": "approval_expired"}


def _loadout_basis(settings: Dict[str, Any], profiles_by_name: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """``{"name", "exists", "changes"}`` for a chat based on a loadout, else None.

    ``changes`` names what the chat's own copy no longer shares with the
    loadout (src/agent_profiles.loadout_changes); a loadout deleted since has
    ``exists: False`` and no changes to count against.
    """
    name = str((settings or {}).get("agent_profile") or "").strip()
    if not name:
        return None
    profile = profiles_by_name.get(name.casefold())
    if profile is None:
        return {"name": name, "exists": False, "changes": []}
    from src import agent_profiles

    return {"name": profile["name"], "exists": True,
            "changes": agent_profiles.loadout_changes(profile, settings or {})}


def setup_agents_routes(session_manager) -> APIRouter:
    router = APIRouter(prefix="/api/agents", tags=["agents"])

    def _owned(request: Request) -> tuple[Optional[str], Dict[str, Any]]:
        user = effective_user(request)
        try:
            return user, session_manager.get_sessions_for_user(user)
        except Exception:
            return user, {}

    def _require_owned(request: Request, session_id: str) -> Optional[str]:
        user, owned = _owned(request)
        if session_id not in owned:
            raise HTTPException(404, "Session not found")
        return user

    def _crew_roles(session_ids: Set[str]) -> Dict[str, Dict[str, Any]]:
        """``{session_id: {id, name, agent_profile}}`` for chats that are a crew
        member's own chat. One query for the whole page, not one per row."""
        if not session_ids:
            return {}
        try:
            from core.database import CrewMember, Session as DbSession, get_db_session

            out: Dict[str, Dict[str, Any]] = {}
            with get_db_session() as db:
                rows = (
                    db.query(DbSession.id, CrewMember.id, CrewMember.name, CrewMember.agent_profile)
                    .join(CrewMember, CrewMember.id == DbSession.crew_member_id)
                    .filter(DbSession.id.in_(list(session_ids)))
                    .all()
                )
            for sid, crew_id, crew_name, profile in rows:
                out[sid] = {"id": crew_id, "name": crew_name, "agent_profile": profile or ""}
            return out
        except Exception:
            logger.debug("crew role lookup failed", exc_info=True)
            return {}

    def _archived_owned(user: Optional[str]) -> Dict[str, Any]:
        """Load only this owner's archived sessions for the archive view.

        ``SessionManager`` intentionally keeps archived chats out of its hot
        in-memory list after restart.  The dashboard therefore cannot rely on
        that cache when the user wants to restore one.
        """
        try:
            from core.database import SessionLocal, Session as DbSession
            db = SessionLocal()
            try:
                rows = db.query(DbSession.id, DbSession.name, DbSession.model).filter(
                    DbSession.owner == user, DbSession.archived == True).all()
            finally:
                db.close()
            # Do not hydrate historical messages during a five-second fleet
            # refresh. Archive cards only need this metadata, and Restore has
            # its own owner check before loading anything.
            return {row.id: SimpleNamespace(id=row.id, name=row.name or row.id,
                                             model=row.model or '', archived=True)
                    for row in rows}
        except Exception:
            return {}

    def _archived_among(user: Optional[str], ids: Set[str]) -> Set[str]:
        """Which of ``ids`` are this owner's archived chats."""
        if not ids:
            return set()
        try:
            from core.database import SessionLocal, Session as DbSession
            db = SessionLocal()
            try:
                return {row.id for row in db.query(DbSession.id).filter(
                    DbSession.owner == user, DbSession.archived == True,  # noqa: E712
                    DbSession.id.in_(list(ids))).all()}
            finally:
                db.close()
        except Exception:
            logger.debug("archived parent lookup failed", exc_info=True)
            return set()

    def _stranded(parents: Dict[str, Any], archived_ids: Set[str]) -> Set[str]:
        """Live chats whose ``parent_session`` ancestry reaches an archived chat.

        Before archive took the whole unit, archiving a parent left its
        workers live; they belong with that parent, not loose in the fleet.
        """
        out: Set[str] = set()
        for sid in parents:
            seen = {sid}
            parent = parents.get(sid)
            while parent and parent not in seen:
                if parent in archived_ids:
                    out.add(sid)
                    break
                seen.add(parent)
                parent = parents.get(parent)
        return out

    def _is_conversation(settings: Dict[str, Any]) -> bool:
        """A chat made with an agent (Talk to agent, or a loadout applied) that
        no other chat started. Workers carry ``parent_session``."""
        return bool(settings.get("agent_profile")) and not settings.get("parent_session")

    def _created_at(ids: Set[str]) -> Dict[str, float]:
        """Creation time of each chat, as epoch seconds (the stable list order)."""
        if not ids:
            return {}
        try:
            from datetime import timezone
            from core.database import SessionLocal, Session as DbSession
            db = SessionLocal()
            try:
                rows = db.query(DbSession.id, DbSession.created_at).filter(DbSession.id.in_(list(ids))).all()
            finally:
                db.close()
            return {row.id: row.created_at.replace(tzinfo=timezone.utc).timestamp()
                    for row in rows if row.created_at}
        except Exception:
            logger.debug("conversation creation times unavailable", exc_info=True)
            return {}

    def _recent_chats(user: Optional[str], owned: Dict[str, Any], limit: int) -> list:
        """The launch form's "Report to chat" list, most recently active first.

        It was the first ``limit`` owned chats in no particular order, so the
        chat the user was just in could be missing from it.
        """
        ordered: list = []
        try:
            from core.database import SessionLocal, Session as DbSession
            db = SessionLocal()
            try:
                query = db.query(DbSession.id).filter(DbSession.archived == False)  # noqa: E712
                if user is not None:
                    query = query.filter(DbSession.owner == user)
                ordered = [row.id for row in query.order_by(DbSession.last_message_at.desc()).limit(limit * 2)]
            finally:
                db.close()
        except Exception:
            logger.debug("recent chat order unavailable", exc_info=True)
        ids = [sid for sid in ordered if sid in owned]
        seen = set(ids)
        ids += [sid for sid, s in owned.items() if sid not in seen and not getattr(s, "archived", False)]
        return [{"id": sid, "name": getattr(owned[sid], "name", "") or sid} for sid in ids[:limit]]

    # Identifies this router's answers in the shared poll cache (tests and
    # embedders build several routers over different session managers).
    _poll_ns = object()

    @router.get("/overview")
    async def overview(request: Request, current_session: Optional[str] = Query(default=None),
                       archived: bool = Query(default=False)):
        user, owned = _owned(request)
        show_archived = archived is True
        # The dashboard polls this on a timer from every open tab. Building it
        # reads session settings and crew rows from SQLite and the activity
        # registry behind a threading lock, per visible chat -- on the event
        # loop that blocked every agent stream whenever SQLite was busy. Build
        # it in a worker thread, once for concurrent identical polls.
        from src.poll_coalesce import coalesced

        key_session = current_session if isinstance(current_session, str) else None
        snapshot = dict(owned)
        can_edit = _can_edit_loadouts(request)
        return await coalesced(
            ("agents.overview", _poll_ns, user, key_session, show_archived, can_edit),
            lambda: _overview_sync(user, snapshot, current_session, show_archived, can_edit),
            ttl=0,
        )

    def _overview_sync(user: Optional[str], owned: Dict[str, Any], current_session: Optional[str],
                       show_archived: bool, can_edit_loadouts: bool = False) -> Dict[str, Any]:
        # Keep cleanup recoverable: archived chats are intentionally absent
        # from the normal fleet, but can be requested by an archive browser.
        # Endpoint functions are also called directly by a few embedders and
        # tests, where FastAPI's Query default is still a Query object.  Only
        # the literal boolean True opts into the archive view (see overview).
        # The launch form reports to a live chat whichever view is open; in
        # the archive view `owned` below holds only archived chats.
        from core.database import get_session_settings
        live_chats = {sid: sess for sid, sess in owned.items() if not getattr(sess, "archived", False)}
        live_settings = {sid: get_session_settings(sid) or {} for sid in live_chats}
        live_parents = {sid: settings.get("parent_session") for sid, settings in live_settings.items()}
        if show_archived:
            owned.update(_archived_owned(user))
            archived_ids = {sid for sid, sess in owned.items() if getattr(sess, "archived", False)}
        else:
            archived_ids = _archived_among(user, {p for p in live_parents.values()
                                                  if isinstance(p, str) and p not in live_chats})
        stranded = _stranded(live_parents, archived_ids)
        owned = {sid: sess for sid, sess in owned.items()
                 if bool(getattr(sess, "archived", False)) == show_archived
                 or (show_archived and sid in stranded)}
        now = time.time()
        chat_runs = {r["session_id"]: r for r in agent_runs.list_runs(set(owned))}
        by_session: Dict[str, list] = {}
        # Runs of workers this chat started. A worker's run is filed under the
        # worker's own chat, so the parent's "Child workers" list never showed
        # a loadout worker (only send_to_session sub-agents, which are filed
        # under the parent). They are listed, but do not make the parent
        # itself read as running.
        delegated: Dict[str, list] = {}
        # Chats that other chats started (a `send_to_session` sub-agent, a
        # loadout worker), keyed by the child: who started it, and its latest
        # run. A child chat whose own run record had aged out of memory was
        # dropped from the fleet while its parent's "Child workers" list still
        # named it, so the tree under the parent was missing agents that the
        # Overview tab listed (2026-09-30).
        link_parent: Dict[str, str] = {}
        target_runs: Dict[str, Dict[str, Any]] = {}
        for rec in activity.list_runs(limit=400):
            sid = rec.get("session_id")
            if rec.get("source") == "odysseus":
                continue
            fin = rec.get("finished_at")
            if rec.get("status") not in ("running", "waiting_approval") and (not fin or now - fin > _RECENT_S):
                continue
            if sid in owned:
                by_session.setdefault(sid, []).append(rec)
            parent_sid = (rec.get("summary") or {}).get("parent_session")
            if parent_sid and parent_sid != sid and parent_sid in owned:
                delegated.setdefault(parent_sid, []).append(rec)
            target = str((rec.get("summary") or {}).get("target_session") or "")
            if target and target in owned:
                starter = parent_sid if parent_sid and parent_sid != target else (sid if sid != target else None)
                if starter and starter in owned:
                    link_parent.setdefault(target, starter)
                    prev = target_runs.get(target)
                    if prev is None or (rec.get("started_at") or 0) > (prev.get("started_at") or 0):
                        target_runs[target] = rec
        pending: Dict[str, int] = {}
        for rec in tool_approvals.tool_approval_store.pending_for_sessions(owner=user, session_ids=set(owned)):
            pending[rec.session_id] = pending.get(rec.session_id, 0) + 1
        from core.database import get_session_settings
        from src import agent_profiles
        profiles, profile_problems = agent_profiles.load_profiles_with_problems()
        profiles_by_name = {p["name"].casefold(): p for p in profiles}

        rows = []
        visible_ids = set(chat_runs) | set(by_session) | set(pending) | set(delegated) | set(link_parent)
        if show_archived:
            # An archive is a recovery surface, not a recent-activity view:
            # show every stored archived agent chat, even if its last run aged
            # out of the active dashboard's 24-hour window.
            visible_ids.update(owned)
        if current_session in owned:
            visible_ids.add(current_session)
        # The activity registry is short-lived; lineage in session settings is
        # durable. Keep the ancestry of live workers and their historical
        # descendants together, including grandchildren after runs expire.
        settings_by_id = {sid: live_settings[sid] if sid in live_settings else get_session_settings(sid) or {}
                          for sid in owned}
        # Agent conversations are durable: every owned, live chat with an agent
        # applied (Talk to agent, or a loadout applied later) that no other chat
        # started. They are listed from the database, whether or not a run
        # record survives, so a conversation does not vanish after 24 hours.
        conversation_ids = {sid for sid, st in settings_by_id.items()
                            if _is_conversation(st) and sid not in link_parent}
        if not show_archived:
            visible_ids.update(conversation_ids)
        parents = {sid: settings.get('parent_session') for sid, settings in settings_by_id.items()}
        changed = True
        while changed:
            before = len(visible_ids)
            visible_ids.update(parent for sid in tuple(visible_ids)
                               if (parent := parents.get(sid)) in owned and parent != sid)
            visible_ids.update(sid for sid, parent in parents.items() if parent in visible_ids and parent != sid)
            changed = len(visible_ids) != before
        crew_roles = _crew_roles(visible_ids & set(owned))
        created = _created_at(conversation_ids & visible_ids)
        for sid in visible_ids:
            sess = owned.get(sid)
            if sess is None:
                continue
            run = chat_runs.get(sid) or _run_from_activity(target_runs.get(sid))
            if run.get("status") == "done":
                # The chat's own record only knows its turn ended. A worker whose
                # report said blocked or partly done ended with that status.
                reported = _run_from_activity(target_runs.get(sid))
                if (reported.get("status") in ("blocked", "partial")
                        and (reported.get("finished_at") or 0) >= (run.get("started_at") or 0)):
                    run = reported
            children = by_session.get(sid, [])
            live_children = [c for c in children if c.get("status") == "running"]
            if pending.get(sid):
                status = "waiting_approval"
            elif run.get("status") == "running":
                status = "running"
            elif run.get("status") in ("done", "error", "stopped", "blocked", "partial"):
                status = {"done": "finished", "error": "failed", "stopped": "stopped",
                          "blocked": "blocked", "partial": "partial"}[run["status"]]
            else:
                status = "idle"
            # What the last turn ended as, kept apart from whether the chat is
            # free for the next message. A conversation whose turn completed is
            # idle, not "finished": it is still there to talk to.
            outcome = {"done": "done", "error": "failed", "stopped": "stopped",
                       "blocked": "blocked", "partial": "partial"}.get(run.get("status"))
            is_conversation = sid in conversation_ids
            if is_conversation and status == "finished":
                status = "idle"
            events = activity.history(sid, limit=12)
            latest = next((e for e in reversed(events)
                           if e.get("kind") in ("tool_start", "tool_result", "message", "status", "run_started")), None)
            settings = settings_by_id[sid]
            # A turn that ended on `Needs user:` lines is waiting on a person,
            # not finished (src/open_needs.py); running and approvals win.
            needs = open_needs.read(settings, since=run.get("started_at"))
            if needs and status in ("finished", "idle", "stopped"):
                status = "needs_input"
                outcome = "needs_input"
            elif status in ("blocked", "partial") and not needs:
                # Kept distinct from finished; the page lists it under "Needs you"
                # when the worker left a `Needs user:` line.
                needs = list(run.get("needs_user") or [])
            hidden_runs = {str(run_id) for run_id in (settings.get("hidden_agent_runs") or [])}
            own_ids = {c.get("run_id") for c in children}
            children = children + [c for c in delegated.get(sid, []) if c.get("run_id") not in own_ids]
            children = [child for child in children if child.get("run_id") not in hidden_runs]
            # A worker's own run is filed under its chat and decides its status
            # above, but it is the chat itself, not a child of it: listing it
            # under "Child workers" offered an Open button back to this chat.
            children = [child for child in children
                        if (child.get("summary") or {}).get("target_session") != sid]
            # A worker run is filed `waiting_approval` when its turn ended on an
            # approval card, and nothing ever rewrites that record. 2026-10-02:
            # a card that had expired (or been superseded) kept showing as
            # "Needs approval" for hours. The live store is the truth: keep the
            # status only while that worker's chat still holds a pending approval.
            children = [
                _child_with_live_approval(child, pending) for child in children
            ]
            from src.session_settings import effective_worker_limit
            config = {
                "agent_profile": settings.get("agent_profile"),
                "agent_instructions": settings.get("agent_instructions"),
                "agent_persona_name": settings.get("agent_persona_name"),
                "agent_temperature": settings.get("agent_temperature"),
                "agent_max_tokens": settings.get("agent_max_tokens"),
                "agent_reasoning_effort": settings.get("agent_reasoning_effort"),
                "approval_mode": settings.get("approval_mode"),
                "disabled_tools": settings.get("disabled_tools") or [],
                "memory_access": settings.get("memory_access", "write"),
                "skill_access": settings.get("skill_access", "all"),
                "skill_names": settings.get("skill_names") or [],
                "model_access": settings.get("model_access", "all"),
                "allowed_models": settings.get("allowed_models") or [],
                "delegation_policy": settings.get("delegation_policy", "explicit"),
                "max_parallel_workers": effective_worker_limit(settings),
                "allowed_mcp_servers": settings.get("allowed_mcp_servers", ["*"]),
                "private_vault_access": bool(settings.get("private_vault_access", False)),
                "shell_access": _shell_mode(settings),
            }
            # Missing means a legacy denylist-only session. Preserve that
            # distinction so the dashboard derives its access mode once;
            # emitting default `all` here would silently broaden old chats.
            if "tool_access" in settings:
                config["tool_access"] = settings.get("tool_access")
            if "enabled_tools" in settings:
                config["enabled_tools"] = settings.get("enabled_tools") or []
            parent_of = parents.get(sid) or link_parent.get(sid)
            if parent_of == sid:
                parent_of = None
            if pending.get(sid):
                outcome = "waiting_approval"
            elif status == "running":
                outcome = None
            rows.append({
                "session_id": sid,
                # A durable parent conversation, as opposed to a worker or an
                # ad-hoc run. `availability` is whether it can take a message;
                # `outcome` is how its last run ended (done, failed, stopped,
                # blocked, partial, needs_input, waiting_approval, or none yet).
                "conversation": is_conversation,
                "availability": ("working" if status == "running"
                                 else "waiting" if status in ("waiting_approval", "needs_input") else "idle"),
                "outcome": outcome,
                "created_at": created.get(sid),
                "name": getattr(sess, "name", "") or sid,
                "model": getattr(sess, "model", ""),
                "archived": bool(getattr(sess, "archived", False)),
                # Not archived itself, but its parent is: Restore brings it
                # back together with that parent.
                "archived_with_parent": sid in stranded,
                "status": status,
                "started_at": run.get("started_at"),
                "finished_at": run.get("finished_at"),
                "source": run.get("source"),
                "latest": (latest or {}).get("title"),
                "latest_ts": (latest or {}).get("ts"),
                "children": [{"run_id": c["run_id"], "title": c.get("title"), "source": c.get("source"),
                              "status": c.get("status"), "started_at": c.get("started_at"),
                              "finished_at": c.get("finished_at"), "summary": c.get("summary") or {}}
                             for c in sorted(children, key=lambda c: c.get("started_at") or 0, reverse=True)[:12]],
                "children_running": len(live_children),
                "pending_approvals": pending.get(sid, 0),
                "needs": needs,
                # The loadout this chat's settings are a copy of, and what the
                # chat changed from it: "based on Lead Engineer (2 changes)".
                "loadout": _loadout_basis(settings, profiles_by_name),
                # `steer_queued` is unchanged and still means "waiting for a
                # turn to pick it up" — it just no longer has to stand in for
                # the whole story, because `steer` carries the recent messages
                # with the state each one reached and why. Capped at five per
                # row: this is a fleet overview, and the full log for one chat
                # is GET /sessions/{id}/steer.
                "steer_queued": len(agent_control.pending_steer(sid)),
                "steer": agent_control.steer_status(sid, limit=5),
                "profile": settings.get("agent_profile"),
                # An unset preference preserves the existing role-derived silhouette.
                "soldier_appearance": settings.get("agamemnon_soldier_appearance")
                    if isinstance(settings.get("agamemnon_soldier_appearance"), str)
                    and settings["agamemnon_soldier_appearance"] in SOLDIER_APPEARANCES else None,
                # The crew member this chat *is*, when it is one, and the agent
                # profile that crew member names. This is the durable role link
                # (crew_members.agent_profile) — unlike `config`, which is this
                # one chat's copy of a loadout. Absent for an ordinary chat.
                "crew": crew_roles.get(sid),
                "parent_session": parent_of,
                "parent_name": getattr(owned.get(parent_of), "name", None),
                "hidden_run_ids": sorted(hidden_runs),
                "approval_mode": settings.get("approval_mode"),
                "is_current": sid == current_session,
                "config": config,
            })
        if stranded and not show_archived:
            # Stranded workers stay with their archived parent in the archive
            # view; the fleet keeps only those with live work under them.
            keep = set()
            for row in rows:
                if row["status"] in ("running", "waiting_approval", "needs_input"):
                    cursor, seen = row["session_id"], set()
                    while cursor and cursor not in seen:
                        seen.add(cursor)
                        keep.add(cursor)
                        cursor = parents.get(cursor)
            rows = [row for row in rows if row["session_id"] not in stranded or row["session_id"] in keep]
        # An idle conversation with a working child stays discoverable: say so.
        working_under: Dict[str, int] = {}
        for row in rows:
            cursor, seen_chain = row["parent_session"], {row["session_id"]}
            while cursor and cursor not in seen_chain:
                if row["status"] in ("running", "waiting_approval"):
                    working_under[cursor] = working_under.get(cursor, 0) + 1
                seen_chain.add(cursor)
                cursor = parents.get(cursor)
        for row in rows:
            row["child_activity"] = max(row["children_running"], working_under.get(row["session_id"], 0))
        order = {"waiting_approval": 0, "needs_input": 0, "running": 1, "blocked": 2, "partial": 2, "failed": 2,
                 "finished": 3, "stopped": 4, "idle": 5}
        # Conversations come first in a fixed order (newest created first): a
        # status change must not move one. Everything else keeps the old
        # urgency order.
        rows.sort(key=lambda r: ((0, -(r.get("created_at") or 0), r["session_id"]) if r["conversation"]
                                 else (1, order.get(r["status"], 9),
                                       -(r.get("latest_ts") or r.get("started_at") or 0))))
        totals = {
            "running": sum(1 for r in rows if r["status"] == "running"),
            "waiting_approval": sum(1 for r in rows if r["status"] == "waiting_approval"),
            "needs_input": sum(1 for r in rows if r["status"] == "needs_input"
                               or (r["status"] in ("blocked", "partial") and r["needs"])),
            "workers_running": sum(r["children_running"] for r in rows),
            "finished_24h": sum(1 for r in rows if r["status"] == "finished"
                                or (r["conversation"] and r["outcome"] == "done"
                                    and now - (r.get("finished_at") or 0) <= _RECENT_S)),
            "failed_24h": sum(1 for r in rows if r["status"] == "failed"),
        }
        from src.agent_tools.claude_code_tools import configured_concurrency
        return {"now": now, "rows": rows, "totals": totals, "profiles": profiles,
                "profile_problems": profile_problems,
                "can_edit_loadouts": can_edit_loadouts,
                "provider_limits": {"claude_code": configured_concurrency()},
                "chats": _recent_chats(user, live_chats, 200)}

    @router.get("/catalog")
    async def catalog(request: Request):
        """Safe capability inventory used by the per-agent loadout editor."""
        user, owned = _owned(request)
        from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
        from src.tool_security import owner_baseline_disabled_tools, blocked_tools_for_owner
        from src.agent_tools.claude_code_tools import configured_concurrency

        def tool_group(name: str) -> str:
            if name.startswith(("read_", "write_", "edit_", "apply_", "grep", "glob", "ls", "get_workspace")):
                return "Files & code"
            if "email" in name or "calendar" in name or "contact" in name:
                return "Communication"
            if name.startswith(("web_", "trigger_research", "manage_research")):
                return "Research"
            if "memory" in name or "skill" in name or "document" in name or "vault" in name:
                return "Knowledge"
            if "session" in name or "delegate" in name or name in {"pipeline", "ask_teacher", "chat_with_model", "message_agent"}:
                return "Agents & models"
            return "Workspace"

        owner_blocked = owner_baseline_disabled_tools(user)
        tools = [{"name": name, "group": tool_group(name), "description": str(desc).split("\n", 1)[0][:240]}
                 for name, desc in sorted(BUILTIN_TOOL_DESCRIPTIONS.items()) if name not in owner_blocked]
        skills = []
        try:
            from services.memory.skills import SkillsManager
            from src.constants import DATA_DIR
            skills = [{"name": item.get("name"), "category": item.get("category") or "general",
                       "description": item.get("description") or ""}
                      for item in SkillsManager(DATA_DIR).index_for(owner=user)]
        except Exception:
            pass
        mcp_servers: Dict[str, Dict[str, Any]] = {}
        try:
            from src.tool_utils import get_mcp_manager
            manager = None if blocked_tools_for_owner(user) else get_mcp_manager()
            for item in manager.get_all_tools() if manager else []:
                sid = str(item.get("server_id") or "")
                row = mcp_servers.setdefault(sid, {"id": sid, "name": item.get("server_name") or sid, "tools": []})
                row["tools"].append({"name": item.get("name"), "description": item.get("description") or ""})
        except Exception:
            pass
        models = sorted({str(getattr(sess, "model", "") or "") for sess in owned.values() if getattr(sess, "model", None)})
        try:
            from src import agent_profiles
            for profile in agent_profiles.load_profiles():
                models.extend([profile.get("model"), *(profile.get("model_fallbacks") or []),
                               *(profile.get("allowed_models") or [])])
        except Exception:
            pass
        return {"tools": tools, "skills": skills, "mcp_servers": list(mcp_servers.values()),
                "models": sorted({str(model) for model in models if model}, key=str.casefold),
                "provider_limits": {"claude_code": configured_concurrency()}}

    @router.get("/stream")
    async def stream(request: Request, since: int = Query(default=0)):
        user, owned = _owned(request)
        owned_ids: Set[str] = set(owned)
        state = {"checked": time.time()}

        async def gen():
            async for frame in activity.subscribe(activity.GLOBAL_FEED, since_seq=max(0, since)):
                # Frames carry an `id:` line holding every session's position;
                # this feed is filtered to the viewer's chats, so the id (which
                # names other sessions) is dropped and only the data is passed on.
                data = next((ln for ln in frame.splitlines() if ln.startswith("data: ")), None)
                if data is None:
                    yield frame
                    continue
                frame = data + "\n\n"
                try:
                    ev = json.loads(data[6:])
                except ValueError:
                    continue
                sid = ev.get("session_id") if isinstance(ev, dict) else None
                if sid and sid not in owned_ids and time.time() - state["checked"] > 5:
                    # A worker launched during the stream is a new chat of ours.
                    state["checked"] = time.time()
                    try:
                        owned_ids.update(session_manager.get_sessions_for_user(user))
                    except Exception:
                        pass
                if not sid or sid in owned_ids or ev.get("type") == "ready":
                    yield frame

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.get("/runs/{run_id}/events")
    async def run_events(request: Request, run_id: str, limit: int = 300):
        rec = activity.get_run(run_id)
        if rec is None:
            raise HTTPException(404, "Run not found")
        _require_owned(request, rec.get("session_id") or "")
        rec["events"] = activity.run_events(run_id, limit=limit)
        return rec

    @router.post("/runs/{run_id}/stop")
    async def stop(request: Request, run_id: str):
        rec = activity.get_run(run_id)
        if rec is None:
            raise HTTPException(404, "Run not found")
        _require_owned(request, rec.get("session_id") or "")
        try:
            return await agent_control.stop_run(run_id, by="from the Agent Control Room")
        except LookupError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @router.post("/sessions/{session_id}/steer")
    async def steer(request: Request, session_id: str):
        user = _require_owned(request, session_id)
        body = await request.json()
        text = str((body or {}).get("text") or "")
        # Every refusal below is recorded against the target chat as a failed
        # steering message before it is raised. The caller sees an error either
        # way; what it buys is that someone reading that agent's timeline later
        # can see a correction was attempted and why it did not land — which
        # used to exist only as a toast in one browser. The reasons say only
        # what is true (the message was not queued as a steer) and not that it
        # was lost: the chat composer answers this same 409 by sending the text
        # as an ordinary next turn (static/js/chat.js), and that delivery shows
        # up in the transcript on its own.
        if not agent_runs.is_busy(session_id):
            agent_control.note_refused(session_id, text, "not queued: the chat was not running", owner=user)
            raise HTTPException(409, "That chat is not running; send it a normal message instead")
        # Only the agent loop drains the queue, and only between rounds. A plain
        # single-shot reply is "busy" but has no rounds, so accepting a steer for
        # one would swallow the message outright. 409 tells the caller to send it
        # as an ordinary turn instead.
        if not agent_control.is_steerable(session_id):
            agent_control.note_refused(session_id, text, "not queued: the chat was not running an agent turn",
                                       owner=user)
            raise HTTPException(409, "That chat is not running an agent turn; send it a normal message instead")
        try:
            rec = agent_control.steer(session_id, text, owner=user)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"queued": True, "id": rec["id"], "state": rec["state"], "text": rec["text"],
                "pending": len(agent_control.pending_steer(session_id))}

    @router.get("/sessions/{session_id}/steer")
    async def steer_log(request: Request, session_id: str, limit: int = 50):
        """Every recent steering message for one chat, with its lifecycle.

        The overview carries the last few per row, which is what the detail
        pane renders; this is the after-the-fact view for one chat — including
        chats the overview has dropped because they have been idle for a day.
        It reads the activity feed, so it answers for messages that left the
        in-memory queue long ago.
        """
        _require_owned(request, session_id)
        return {"session_id": session_id,
                **agent_control.steer_status(session_id, limit=max(1, min(int(limit or 50), 200)))}

    @router.get("/sessions/{session_id}/checklist")
    async def checklist(request: Request, session_id: str):
        """The chat's task checklist (src/task_checklist.py), for the panel
        above the composer. The live copy arrives as `plan_update` events; this
        is what a chat shows when it is opened or reloaded."""
        _require_owned(request, session_id)
        from src import task_checklist

        record = task_checklist.load(session_id)
        if not record:
            return {"session_id": session_id, "plan": "", "items": [], "done": 0, "total": 0}
        plan = str(record.get("plan") or "")
        done, total = task_checklist.counts(plan)
        return {"session_id": session_id, "plan": plan, "items": task_checklist.items(plan),
                "done": done, "total": total, "updated_at": record.get("updated_at")}

    @router.get("/approvals")
    async def approvals(request: Request):
        user, owned = _owned(request)
        rows = []
        # Listed only: an exact approval is decided on its card in the chat,
        # where the sealed action is shown in full.
        for rec in tool_approvals.tool_approval_store.pending_for_sessions(owner=user, session_ids=set(owned)):
            sess = owned.get(rec.session_id)
            rows.append({
                "id": rec.approval_id,
                "session_id": rec.session_id,
                "session_name": getattr(sess, "name", "") if sess else "",
                "tool": rec.tool_name,
                "command": rec.content,
                "reason": rec.reason or ("Untrusted content influenced this run"
                                         if rec.external_untrusted_context_seen
                                         else "Waiting for an exact approval"),
                "created_at": rec.created_at,
                "expires_at": rec.expires_at,
            })
        return {"approvals": rows}

    @router.get("/approvals/{approval_id}/status")
    async def approval_status(request: Request, approval_id: str, session_id: str):
        """Whether one approval card can still be answered.

        A card restored from history, or left open past its window, asks here
        before it enables its buttons. ``pending`` is the only answerable
        state; ``expired`` also covers an id the server no longer holds.
        """
        user = _require_owned(request, session_id)
        state = tool_approvals.tool_approval_store.status(
            approval_id, owner=user, session_id=session_id,
        )
        rec = tool_approvals.tool_approval_store.peek(approval_id) if state == "pending" else None
        return {
            "approval_id": approval_id,
            "status": state,
            "expires_at": rec.expires_at if rec is not None else None,
        }

    @router.post("/sessions/{session_id}/crew_profile")
    async def set_crew_profile(request: Request, session_id: str):
        """Point this chat's crew member at a named agent profile (or clear it).

        One role, one definition: the same link is what scopes that crew
        member's scheduled tasks, so this is not a per-chat override — it is
        editable here because this is where the user already looks at what an
        agent may do.
        """
        user = _require_owned(request, session_id)
        body = await request.json()
        name = str((body or {}).get("profile") or "").strip()
        if name:
            from src import agent_profiles

            found = agent_profiles.get_profile(name)
            if found is None:
                raise HTTPException(400, f"No agent profile named {name!r}")
            # Store the profile's own spelling (get_profile matches case-
            # insensitively) so the link reads back as the select's option value.
            name = found["name"]
        from core.database import CrewMember, Session as DbSession, get_db_session

        with get_db_session() as db:
            crew_id = db.query(DbSession.crew_member_id).filter(DbSession.id == session_id).scalar()
            if not crew_id:
                raise HTTPException(404, "This chat does not belong to an agent")
            crew = db.query(CrewMember).filter(CrewMember.id == crew_id).first()
            if crew is None or (crew.owner and user and crew.owner != user):
                raise HTTPException(404, "This chat does not belong to an agent")
            crew.agent_profile = name or None
        # Push the new role onto the chat immediately, so the loadout the user
        # is looking at is the one the next turn will run under.
        try:
            from src.crew_profile import apply_crew_profile_to_session

            apply_crew_profile_to_session(session_id)
        except Exception:
            logger.warning("crew profile sync failed for %s", session_id, exc_info=True)
        return {"session_id": session_id, "crew_member_id": crew_id, "profile": name}


    @router.post("/sessions/{session_id}/archive")
    async def archive_session(request: Request, session_id: str):
        from src.agent_lifecycle import change_archive
        return change_archive(session_id, effective_user(request), session_manager)


    @router.post("/sessions/{session_id}/needs/clear")
    async def clear_needs(request: Request, session_id: str):
        """Mark a chat's "Needs your input" as answered (for a question the
        person settled somewhere other than in that chat)."""
        _require_owned(request, session_id)
        return {"ok": True, "session_id": session_id, "cleared": open_needs.clear(session_id)}


    @router.post("/sessions/{session_id}/unarchive")
    async def unarchive_session(request: Request, session_id: str):
        from src.agent_lifecycle import change_archive
        return change_archive(session_id, effective_user(request), session_manager, restore=True)


    @router.post("/sessions/{session_id}/cleanup-runs")
    async def cleanup_runs(request: Request, session_id: str):
        """Hide completed child-run cards without deleting their audit trail."""
        _require_owned(request, session_id)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected an object containing run_ids")
        raw_ids = (body or {}).get("run_ids") or []
        if not isinstance(raw_ids, list) or len(raw_ids) > 100:
            raise HTTPException(400, "Choose up to 100 completed runs to hide")
        requested = {str(run_id) for run_id in raw_ids if str(run_id)}
        if not requested:
            raise HTTPException(400, "Choose one or more completed runs to hide")
        terminal = set()
        for run_id in requested:
            rec = activity.get_run(run_id)
            if rec is None or rec.get("session_id") != session_id:
                raise HTTPException(404, "Run not found")
            if rec.get("status") not in {"completed", "finished", "failed", "error", "cancelled", "stopped", "incomplete", "interrupted",
                                         "blocked", "partial"}:
                raise HTTPException(409, "Only completed work can be hidden")
            terminal.add(run_id)
        from core.database import get_session_settings, update_session_settings
        settings = get_session_settings(session_id)
        hidden = {str(run_id) for run_id in (settings.get("hidden_agent_runs") or [])}
        hidden.update(terminal)
        # Bounded, recoverable preference only. The original activity JSONL
        # and run registry are intentionally untouched.
        saved = update_session_settings(session_id, {"hidden_agent_runs": sorted(hidden)[-300:]})
        if saved is None:
            raise HTTPException(500, "Could not save cleanup preference")
        return {"ok": True, "hidden_run_ids": sorted(terminal), "history_preserved": True}

    @router.post("/sessions/{session_id}/restore-runs")
    async def restore_runs(request: Request, session_id: str):
        """Put previously hidden completed-run cards back in this fleet row."""
        _require_owned(request, session_id)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected an object containing run_ids")
        raw_ids = (body or {}).get("run_ids") or []
        # Match the persisted preference cap so Restore cards can undo the
        # entire visible cleanup preference in one action.
        if not isinstance(raw_ids, list) or len(raw_ids) > 300:
            raise HTTPException(400, "Choose up to 300 hidden runs to restore")
        requested = {str(run_id) for run_id in raw_ids if str(run_id)}
        from core.database import get_session_settings, update_session_settings
        settings = get_session_settings(session_id)
        hidden = {str(run_id) for run_id in (settings.get("hidden_agent_runs") or [])}
        # Only remove ids already hidden for this chat. No run records or
        # events are touched, and arbitrary ids from another owner are inert.
        restored = hidden & requested
        saved = update_session_settings(session_id, {"hidden_agent_runs": sorted(hidden - restored)})
        if saved is None:
            raise HTTPException(500, "Could not save cleanup preference")
        return {"ok": True, "restored_run_ids": sorted(restored), "history_preserved": True}

    # The per-chat keys a loadout writes (agent_profiles.session_patch). Clearing
    # a loadout removes these; approval_mode is left alone because the chat's
    # settings panel also sets it, and a stricter leftover mode is harmless.
    _LOADOUT_KEYS = ("agent_profile", "agent_instructions", "agent_persona_name",
                     "agent_temperature", "agent_max_tokens", "agent_reasoning_effort",
                     "tool_access", "enabled_tools",
                     "disabled_tools", "memory_access", "skill_access", "skill_names",
                     "model_access", "allowed_models", "delegation_policy",
                     "max_parallel_workers", "allowed_mcp_servers", "private_vault_access",
                     "shell_access")

    @router.get("/profiles")
    async def profiles(request: Request):
        """Configured loadouts, trimmed to what a picker shows."""
        _owned(request)
        from src import agent_profiles
        return {"profiles": [{"name": p["name"], "description": p.get("description") or "",
                              "model": p.get("model") or "", "tool_access": p.get("tool_access", "all"),
                              "persona_name": p.get("persona_name") or "",
                              "temperature": p.get("temperature"),
                              "memory_access": p.get("memory_access", "read"),
                              "delegation_policy": p.get("delegation_policy", "explicit"),
                              "quick_actions": list(p.get("quick_actions") or [])}
                             for p in agent_profiles.load_profiles()]}

    def _starter_policies(user: Optional[str], session_id: str) -> tuple:
        """``(owner ceiling, starting chat's policy)``. A chat whose policy cannot
        be read is treated as a top-level chat of the owner rather than failing
        the screen; launching re-reads it strictly."""
        from src import agent_loadouts

        top = agent_loadouts.caller_policy(None, user)
        if not session_id:
            return top, top
        try:
            return top, agent_loadouts.caller_policy(session_id, user)
        except ValueError:
            return top, top

    def _split_names(raw: Any) -> list:
        if isinstance(raw, str):
            raw = raw.split(",")
        if not isinstance(raw, (list, tuple)):
            return []
        return sorted({str(item).strip() for item in raw if str(item).strip()})

    def _grantable(needed: list, profile: Dict[str, Any], starter: Dict[str, Any]) -> list:
        """Of ``needed``, the tools the starting chat holds itself and the agent lacks.

        The one place "allow for this run" is decided, so it can never offer
        more than the chat starting the task already has.
        """
        from src import agent_profiles

        if (profile.get("tool_access") or "all") == "all":
            return []
        held = agent_profiles.expand_tool_aliases(starter.get("allowed_tools") or ())
        return [t for t in needed if t in held]

    def _blocker_from_payload(payload: Dict[str, Any], profile: Dict[str, Any],
                              starter: Dict[str, Any]) -> Dict[str, Any]:
        from src import blockers

        blocker = blockers.from_preflight(payload)
        problems = payload.get("problems") or []
        first = problems[0] if problems and isinstance(problems[0], dict) else {}
        needed = list((payload.get("next_action") or {}).get("enable_tools") or [])
        return blockers.public(blocker, blockers.recovery_actions(
            blocker.code, candidates=list(first.get("candidates") or []), needed_tools=needed,
            grantable_tools=_grantable(needed, profile, starter)))

    def _document_blocker(gap: Dict[str, Any], profile: Dict[str, Any], starter: Dict[str, Any]) -> Dict[str, Any]:
        from src import blockers

        # read_file is left out: it cannot open an editor document or the logs,
        # and on a vault note it needs a private-notes grant the run does not get.
        needed = [t for t in gap.get("tools") or () if t != "read_file"][:1]
        return blockers.public(blockers.from_document_gap(gap), blockers.recovery_actions(
            "DOCUMENT_UNREADABLE", needed_tools=needed, grantable_tools=_grantable(needed, profile, starter)))

    def _one_off(profile: Dict[str, Any], extra: list, starter: Dict[str, Any]):
        """``(profile for this run, granted, refused)`` with ``extra`` tools added."""
        from src.agent_tools.loadout_tools import _one_off_profile

        one_off, granted, refused = _one_off_profile(profile, extra, starter)
        return (one_off or profile), granted, refused

    @router.get("/profiles/{name}/access")
    async def profile_access(request: Request, name: str, session_id: str = Query(default=""),
                             task: str = Query(default=""), workspace: str = Query(default=""),
                             requires: str = Query(default=""), extra_tools: str = Query(default="")):
        """What this agent can do when you talk to it and when you assign it a
        task, in plain sentences, and (with ``task``) what would stop the task.

        Read-only and owner-scoped: ``session_id`` must be one of the caller's
        chats. ``workspace``, ``requires`` and ``extra_tools`` re-check after the
        person took a recovery action.
        """
        import asyncio

        from src import agent_loadouts, agent_profiles, loadout_access_summary, worker_preflight

        user, owned = _owned(request)
        session_id = session_id.strip()
        if session_id and session_id not in owned:
            raise HTTPException(404, "Session not found")
        profile = agent_profiles.get_profile(name)
        if not profile:
            raise HTTPException(404, f"No agent named {name!r}")
        top, starter = _starter_policies(user, session_id)
        try:
            capped, _notes = agent_loadouts.cap_to_starter(profile, session_id or None, user, policy=starter)
        except ValueError:
            capped = profile
        effective = capped or profile
        extra = _split_names(extra_tools)
        if extra:
            effective, _granted, _refused = _one_off(effective, extra, starter)
        body = loadout_access_summary.build_access(
            profile, talk_allowed=top["allowed_tools"], starter_allowed=starter["allowed_tools"],
            worker_depth=starter.get("worker_depth", 0))
        if effective is not profile:
            assign = loadout_access_summary.build_access(
                effective, talk_allowed=top["allowed_tools"], starter_allowed=starter["allowed_tools"],
                worker_depth=starter.get("worker_depth", 0))
            body["assign"] = assign["assign"]
        task = task.strip()
        if task:
            from src.agent_tools.session_tools import _caller_workspace

            checked = await asyncio.to_thread(
                lambda: worker_preflight.run_preflight(
                    task, explicit_workspace=workspace.strip() or None,
                    inherited_workspace=_caller_workspace(session_id or None),
                    unavailable_tools=worker_preflight.worker_unavailable_tools(user, effective),
                    requires=_split_names(requires), loadout_name=profile["name"]))
            if not checked.ok:
                body["assign"]["blocker"] = _blocker_from_payload(checked.blocked_payload(), effective, starter)
            else:
                gap = worker_preflight.document_gap(task, effective)
                if gap:
                    body["assign"]["blocker"] = _document_blocker(gap, effective, starter)
        return body

    @router.post("/profiles/{name}/chat")
    async def chat_with_profile(request: Request, name: str):
        """Open a new chat of the caller's with this agent applied ("Talk to agent").

        The chat gets exactly what ``POST /sessions/{id}/loadout`` would give it,
        under the same guard. It runs on the agent's own model when it has one;
        otherwise on the model of ``from_session`` (one of the caller's chats),
        else on the model of the caller's latest chat.
        """
        import asyncio
        import uuid

        from src import agent_profiles, shell_access
        from src.ai_interaction import _resolve_model

        user, owned = _owned(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        profile = agent_profiles.get_profile(name)
        if not profile:
            raise HTTPException(404, f"No agent named {name!r}")
        from_session = str(body.get("from_session") or "").strip()
        if from_session and from_session not in owned:
            raise HTTPException(404, "Session not found")
        patch = agent_profiles.session_patch(profile)
        sid = str(uuid.uuid4())
        shell_access.guard_settings_change(request, sid, patch)

        resolved = None
        for spec in [profile.get("model"), *(profile.get("model_fallbacks") or [])]:
            if not spec:
                continue
            try:
                resolved = await asyncio.to_thread(_resolve_model, spec, user)
                break
            except ValueError:
                continue
        if resolved is None:
            donor = owned.get(from_session) if from_session else None
            if donor is None:
                usable = [s for s in owned.values()
                          if getattr(s, "model", "") and getattr(s, "endpoint_url", "")]
                donor = usable[-1] if usable else None
            if donor is None:
                raise HTTPException(400, "There is no model to start this chat on. Open any chat once, "
                                         "or give this agent a model.")
            resolved = (donor.endpoint_url, donor.model, getattr(donor, "headers", None))
        url, model, headers = resolved
        session = session_manager.create_session(session_id=sid, name=profile["name"], endpoint_url=url,
                                                 model=model, rag=False, owner=user)
        if headers:
            session.headers = headers
            from routes.session_routes import _persist_session_headers
            _persist_session_headers(sid, headers)
        from core.database import update_session_settings

        if update_session_settings(sid, patch) is None:
            try:
                session_manager.delete_session(sid)
            except Exception:
                logger.debug("could not remove the half-made chat %s", sid, exc_info=True)
            raise HTTPException(500, "Could not set up the chat with this agent")
        return {"session_id": sid, "name": profile["name"], "model": model}

    def _can_edit_loadouts(request: Request) -> bool:
        try:
            _require_profile_admin(request)
            return True
        except HTTPException:
            return False
        except Exception:
            return False

    def _require_profile_admin(request: Request) -> str:
        """The same gate as saving profiles through ``POST /api/auth/settings``:
        an admin in a browser session (or the single-user deployment). A bearer
        token is refused; that route never accepted one either."""
        from src.auth_helpers import is_delegated_credential, require_user
        from src.tool_security import owner_is_admin_or_single_user

        if is_delegated_credential(request):
            raise HTTPException(403, "Admin only")
        user = require_user(request)
        if not owner_is_admin_or_single_user(user):
            raise HTTPException(403, "Admin only")
        return user or ""

    @router.get("/profiles/export")
    async def export_profiles(request: Request, names: Optional[str] = Query(default=None)):
        """The configured loadouts as a portable JSON download."""
        _require_profile_admin(request)
        from fastapi.responses import JSONResponse
        from src import agent_profile_transfer
        try:
            document = agent_profile_transfer.export_profiles(names)
        except ValueError as exc:
            raise HTTPException(404, str(exc))
        stamp = document["exported_at"][:10]
        return JSONResponse(document, headers={
            "Content-Disposition": f'attachment; filename="agamemnon-agent-profiles-{stamp}.json"'})

    @router.post("/profiles/import")
    async def import_profiles(request: Request):
        """Store loadouts from an export. Body: the export document itself (with
        ``?mode=`` / ``?rename_conflicts=``), or ``{"document": ..., "mode":
        "merge"|"replace", "rename_conflicts": bool}``."""
        user = _require_profile_admin(request)
        from src import agent_profile_transfer
        from src.agent_tools.loadout_tools import _model_problem
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Expected a JSON body")
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object")
        params = request.query_params
        wrapped = "document" in body and "format" not in body
        document = body.get("document") if wrapped else body
        mode = (body.get("mode") if wrapped else None) or params.get("mode") or "merge"
        rename = body.get("rename_conflicts") if wrapped else None
        if rename is None:
            rename = str(params.get("rename_conflicts") or "").strip().lower() in ("1", "true", "yes", "on")
        try:
            report = agent_profile_transfer.import_profiles(
                document, mode=str(mode), rename_conflicts=bool(rename),
                check_model=lambda spec: _model_problem(spec, user or None))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        from src import agent_profiles
        return {"ok": report["written"] or not report["errors"],
                "message": agent_profile_transfer.summary_line(report),
                "report": report, "profiles": agent_profiles.load_profiles()}

    @router.post("/sessions/{session_id}/appearance")
    async def set_appearance(request: Request, session_id: str):
        """Persist a root agent chat's Agamemnon-only visual preference."""
        _require_owned(request, session_id)
        from core.database import get_session_settings, update_session_settings
        if (get_session_settings(session_id) or {}).get("parent_session"):
            raise HTTPException(400, "Appearance can only be set on a parent agent")
        try:
            body = await request.json()
        except (ValueError, TypeError):
            raise HTTPException(400, "Expected an appearance")
        appearance = body.get("appearance") if isinstance(body, dict) else None
        if appearance is not None and (not isinstance(appearance, str) or appearance not in SOLDIER_APPEARANCES):
            raise HTTPException(400, "Unknown soldier appearance")
        saved = update_session_settings(session_id, {"agamemnon_soldier_appearance": appearance})
        if saved is None:
            raise HTTPException(500, "Could not save appearance")
        return {"appearance": appearance}

    @router.get("/profiles/templates")
    async def list_profile_templates(request: Request):
        """Loadout templates of the integrations that are available now."""
        _require_profile_admin(request)
        from src import agent_profile_transfer
        return {"templates": agent_profile_transfer.list_templates()}

    @router.post("/profiles/templates/install")
    async def install_profile_template(request: Request):
        """Import one shipped template. Same auth and validation as ``/profiles/import``."""
        user = _require_profile_admin(request)
        from src import agent_profile_transfer, agent_profiles
        from src.agent_tools.loadout_tools import _model_problem
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Expected a JSON body")
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object")
        try:
            report = agent_profile_transfer.install_template(
                str(body.get("integration") or "").strip(), str(body.get("template") or "").strip(),
                overwrite=bool(body.get("overwrite")),
                check_model=lambda spec: _model_problem(spec, user or None))
        except ValueError as exc:
            message = str(exc)
            raise HTTPException(409 if "already exists" in message else 404, message)
        return {"ok": report["written"] or not report["errors"],
                "message": agent_profile_transfer.summary_line(report),
                "report": report, "profiles": agent_profiles.load_profiles()}

    @router.post("/sessions/{session_id}/loadout")
    async def apply_loadout(request: Request, session_id: str):
        """Run this chat under a loadout, or clear it with ``{"profile": null}``."""
        _require_owned(request, session_id)
        from core.database import update_session_settings
        from src import agent_profiles
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = str((body or {}).get("profile") or "").strip()
        if name:
            profile = agent_profiles.get_profile(name)
            if not profile:
                raise HTTPException(404, f"No loadout named {name!r}")
            patch = agent_profiles.session_patch(profile)
            from src import shell_access
            shell_access.guard_settings_change(request, session_id, patch)
        else:
            patch = {key: None for key in _LOADOUT_KEYS}
        saved = update_session_settings(session_id, patch)
        if saved is None:
            raise HTTPException(500, "Could not save the chat's loadout")
        return {"ok": True, "agent_profile": saved.get("agent_profile")}

    @router.post("/sessions/{session_id}/save-to-loadout")
    async def save_to_loadout(request: Request, session_id: str):
        """Make this chat's settings the settings of a loadout.

        Body: ``{}`` saves into the loadout the chat is based on;
        ``{"new_name": "..."}`` creates a loadout from the chat and bases the
        chat on it. Admin only, like editing loadouts anywhere else. Other chats
        based on the loadout take on each changed setting they have not changed
        themselves (``propagate_profile_edits``), exactly as a save of the
        loadout list does.
        """
        _require_owned(request, session_id)
        _require_profile_admin(request)
        from core.database import get_session_settings, update_session_settings
        from src import agent_loadouts, agent_profiles
        try:
            body = await request.json()
        except Exception:
            body = {}
        settings = get_session_settings(session_id) or {}
        new_name = str((body or {}).get("new_name") or "").strip()
        old_list = agent_profiles.load_profiles()
        if new_name:
            if any(p["name"].casefold() == new_name.casefold() for p in old_list):
                raise HTTPException(409, f"A loadout named {new_name!r} already exists")
            base = {"name": new_name}
            index = len(old_list)
        else:
            name = str(settings.get("agent_profile") or "").strip()
            if not name:
                raise HTTPException(400, "This chat is not based on a loadout; give the new loadout a name")
            index = next((i for i, p in enumerate(old_list) if p["name"].casefold() == name.casefold()), None)
            if index is None:
                raise HTTPException(404, f"No loadout named {name!r}")
            base = old_list[index]
        candidate = agent_profiles.profile_from_session(base, settings)
        new_list = old_list[:index] + [candidate] + old_list[index + 1:]
        try:
            saved = agent_profiles.validate_profiles(new_list)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        agent_loadouts._write(saved)
        profile = saved[index]
        if new_name:
            update_session_settings(session_id, {"agent_profile": profile["name"]})
            updated: Dict[str, int] = {}
        else:
            updated = agent_profiles.propagate_profile_edits(old_list, saved)
        logger.info("[agent-profiles] %s loadout %r from chat %s; other chats updated: %s",
                    "created" if new_name else "saved", profile["name"], session_id[:8], updated or 0)
        return {"ok": True, "profile": profile, "created": bool(new_name),
                "chats_updated": sum(updated.values())}

    @router.post("/launch")
    async def launch(request: Request):
        user, owned = _owned(request)
        from fastapi.responses import JSONResponse

        from src import agent_loadouts, agent_profiles, blockers, worker_preflight

        body = await request.json()
        body = body if isinstance(body, dict) else {}
        parent = str(body.get("parent_session") or "").strip() or None
        if parent and parent not in owned:
            raise HTTPException(404, "Parent chat not found")
        task = str(body.get("task") or "")
        profile_name = str(body.get("profile") or "").strip() or None
        model = str(body.get("model") or "").strip() or None
        for field in ("requires", "extra_tools"):
            if body.get(field) is not None and not isinstance(body.get(field), (list, str)):
                raise HTTPException(400, f"{field} must be a list of names")
        requires = _split_names(body.get("requires"))
        extra = _split_names(body.get("extra_tools"))

        def refused(blocker: Dict[str, Any]):
            return JSONResponse(status_code=409, content={
                "error": blocker["summary"], "code": blocker["code"], "blocker": blocker})

        launch_kwargs: Dict[str, Any] = {
            "owner": user, "task": task, "profile_name": profile_name, "parent_session": parent,
            "model": model, "workspace": str(body.get("workspace") or "").strip() or None,
            "requires": requires,
        }
        starter = None
        profile = agent_profiles.get_profile(profile_name) if profile_name else None
        if extra or (profile and task.strip()):
            try:
                _top, starter = _starter_policies(user, parent or "")
            except Exception:
                starter = None
        if extra:
            if not profile:
                raise HTTPException(400, "Extra tools are added to a named agent for one run; choose an agent")
            if starter is None:
                raise HTTPException(503, "Could not read this chat's permissions; nothing was allowed")
            base = profile
            try:
                base, _notes = agent_loadouts.cap_to_starter(profile, parent, user, policy=starter)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            one_off, granted, denied = _one_off(base or profile, extra, starter)
            if not granted:
                raise HTTPException(400, "This chat can't allow those tools for the agent: it doesn't have "
                                         "them itself.")
            if one_off is not profile and one_off is not base:
                launch_kwargs.update(profile_name=None, model=None,
                                     inline_profile={**one_off, **({"model": model} if model else {})})
                profile = one_off
        if profile and task.strip() and not body.get("start_anyway"):
            gap = worker_preflight.document_gap(task, profile)
            if gap:
                return refused(_document_blocker(gap, profile, starter or _starter_policies(user, parent or "")[1]))
        try:
            return await agent_control.launch_worker(**launch_kwargs)
        except worker_preflight.WorkerBlocked as exc:
            if starter is None:
                _top, starter = _starter_policies(user, parent or "")
            return refused(_blocker_from_payload(exc.payload, profile or {}, starter))
        except ValueError as exc:
            text = str(exc)
            if text.startswith("worker nesting limit reached"):
                blocker = blockers.Blocker(code="WORKER_DEPTH", summary=text, recovery="user",
                                           user_action="Do this part in the chat that started it.")
                return refused(blockers.public(blocker, blockers.recovery_actions("WORKER_DEPTH")))
            raise HTTPException(400, _launch_error_text(text))
        except RuntimeError as exc:
            raise HTTPException(503, f"The worker could not be started: {exc}")

    def _launch_error_text(text: str) -> str:
        """The launcher's errors are worded for the tool API; say them for the form."""
        if "needs a profile with a model or a calling chat" in text:
            return ("The worker has no model to run on. Choose a loadout that has a model, pick a "
                    "chat to report to (the worker uses its model), or enter a model override.")
        if text.startswith("no agent profile named"):
            return (f"{text[0].upper()}{text[1:]}. It may have been renamed or deleted, or it no longer "
                    "validates; check Settings › Agents.")
        if text == "task is empty":
            return "Describe the task first."
        return text

    @router.get("/history")
    async def history(request: Request, q: str = Query(default=""), limit: int = Query(default=40),
                      offset: int = Query(default=0)):
        """This owner's agent chats, most recently active first, however old.

        The fleet lists a chat only while it runs or for a while after, so an
        agent chat from yesterday could be reached only through the sidebar.
        These are chats that ran in Agent mode, carry a loadout, or are a
        worker (``parent_session``); ``q`` filters by name, loadout or task.
        """
        user, owned = _owned(request)
        limit = max(1, min(int(limit or 40), 100))
        offset = max(0, int(offset or 0))
        needle = str(q or "").strip().casefold()
        try:
            from core.database import SessionLocal, Session as DbSession
            db = SessionLocal()
            try:
                query = db.query(
                    DbSession.id, DbSession.name, DbSession.model, DbSession.mode, DbSession.settings_json,
                    DbSession.last_message_at, DbSession.updated_at, DbSession.message_count,
                ).filter(DbSession.archived == False)  # noqa: E712
                if user is not None:
                    query = query.filter(DbSession.owner == user)
                rows = query.order_by(DbSession.last_message_at.desc()).limit(2000).all()
            finally:
                db.close()
        except Exception:
            logger.debug("agent history query failed", exc_info=True)
            rows = []
        names = {row.id: (row.name or row.id) for row in rows}
        items = []
        for row in rows:
            if row.id not in owned and user is not None:
                continue
            try:
                settings = json.loads(row.settings_json) if row.settings_json else {}
            except (TypeError, ValueError):
                settings = {}
            settings = settings if isinstance(settings, dict) else {}
            profile = str(settings.get("agent_profile") or "")
            parent = str(settings.get("parent_session") or "")
            if row.mode != "agent" and not profile and not parent:
                continue
            item = {
                "id": row.id, "name": row.name or row.id, "model": row.model or "",
                "profile": profile, "parent_session": parent,
                "parent_name": names.get(parent, "") if parent else "",
                "message_count": int(row.message_count or 0),
                "last_active": (row.last_message_at or row.updated_at).isoformat()
                if (row.last_message_at or row.updated_at) else "",
                "workspace": str(settings.get("workspace") or ""),
            }
            if needle and needle not in " ".join(
                    (item["name"], item["profile"], item["parent_name"], item["workspace"])).casefold():
                continue
            items.append(item)
        return {"chats": items[offset:offset + limit], "total": len(items), "offset": offset, "limit": limit}

    return router
