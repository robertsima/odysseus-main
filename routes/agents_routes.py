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
from src import agent_control, agent_runs, tool_approvals
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)

_RECENT_S = 24 * 3600
_LIVE_SOURCES = {"session", "claude_code", "bg_job", "pipeline", "worktree"}


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

    @router.get("/overview")
    async def overview(request: Request, current_session: Optional[str] = Query(default=None),
                       archived: bool = Query(default=False)):
        user, owned = _owned(request)
        # Keep cleanup recoverable: archived chats are intentionally absent
        # from the normal fleet, but can be requested by an archive browser.
        # Endpoint functions are also called directly by a few embedders and
        # tests, where FastAPI's Query default is still a Query object.  Only
        # the literal boolean True opts into the archive view.
        show_archived = archived is True
        if show_archived:
            owned.update(_archived_owned(user))
        owned = {sid: sess for sid, sess in owned.items()
                 if bool(getattr(sess, "archived", False)) == show_archived}
        now = time.time()
        chat_runs = {r["session_id"]: r for r in agent_runs.list_runs(set(owned))}
        by_session: Dict[str, list] = {}
        for rec in activity.list_runs(limit=400):
            sid = rec.get("session_id")
            if sid not in owned or rec.get("source") == "odysseus":
                continue
            fin = rec.get("finished_at")
            if rec.get("status") != "running" and (not fin or now - fin > _RECENT_S):
                continue
            by_session.setdefault(sid, []).append(rec)
        pending: Dict[str, int] = {}
        for rec in tool_approvals.tool_approval_store.pending_for_sessions(owner=user, session_ids=set(owned)):
            pending[rec.session_id] = pending.get(rec.session_id, 0) + 1
        from core.database import get_session_settings

        rows = []
        visible_ids = set(chat_runs) | set(by_session) | set(pending)
        if show_archived:
            # An archive is a recovery surface, not a recent-activity view:
            # show every stored archived agent chat, even if its last run aged
            # out of the active dashboard's 24-hour window.
            visible_ids.update(owned)
        if current_session in owned:
            visible_ids.add(current_session)
        for sid in visible_ids:
            sess = owned.get(sid)
            if sess is None:
                continue
            run = chat_runs.get(sid) or {}
            children = by_session.get(sid, [])
            live_children = [c for c in children if c.get("status") == "running"]
            if pending.get(sid):
                status = "waiting_approval"
            elif run.get("status") == "running" or live_children:
                status = "running"
            elif run.get("status") in ("done", "error", "stopped"):
                status = {"done": "finished", "error": "failed", "stopped": "stopped"}[run["status"]]
            else:
                status = "idle"
            events = activity.history(sid, limit=12)
            latest = next((e for e in reversed(events)
                           if e.get("kind") in ("tool_start", "tool_result", "message", "status", "run_started")), None)
            settings = get_session_settings(sid)
            hidden_runs = {str(run_id) for run_id in (settings.get("hidden_agent_runs") or [])}
            children = [child for child in children if child.get("run_id") not in hidden_runs]
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
            }
            # Missing means a legacy denylist-only session. Preserve that
            # distinction so the dashboard derives its access mode once;
            # emitting default `all` here would silently broaden old chats.
            if "tool_access" in settings:
                config["tool_access"] = settings.get("tool_access")
            if "enabled_tools" in settings:
                config["enabled_tools"] = settings.get("enabled_tools") or []
            rows.append({
                "session_id": sid,
                "name": getattr(sess, "name", "") or sid,
                "model": getattr(sess, "model", ""),
                "archived": bool(getattr(sess, "archived", False)),
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
                # `steer_queued` is unchanged and still means "waiting for a
                # turn to pick it up" — it just no longer has to stand in for
                # the whole story, because `steer` carries the recent messages
                # with the state each one reached and why. Capped at five per
                # row: this is a fleet overview, and the full log for one chat
                # is GET /sessions/{id}/steer.
                "steer_queued": len(agent_control.pending_steer(sid)),
                "steer": agent_control.steer_status(sid, limit=5),
                "profile": settings.get("agent_profile"),
                "parent_session": settings.get("parent_session"),
                "parent_name": getattr(owned.get(settings.get("parent_session")), "name", None),
                "hidden_run_ids": sorted(hidden_runs),
                "approval_mode": settings.get("approval_mode"),
                "is_current": sid == current_session,
                "config": config,
            })
        order = {"waiting_approval": 0, "running": 1, "failed": 2, "finished": 3, "stopped": 4, "idle": 5}
        rows.sort(key=lambda r: (order.get(r["status"], 9), -(r.get("latest_ts") or r.get("started_at") or 0)))
        totals = {
            "running": sum(1 for r in rows if r["status"] == "running"),
            "waiting_approval": sum(1 for r in rows if r["status"] == "waiting_approval"),
            "workers_running": sum(r["children_running"] for r in rows),
            "finished_24h": sum(1 for r in rows if r["status"] in ("finished",)),
            "failed_24h": sum(1 for r in rows if r["status"] == "failed"),
        }
        from src import agent_profiles

        from src.agent_tools.claude_code_tools import configured_concurrency
        return {"now": now, "rows": rows, "totals": totals, "profiles": agent_profiles.load_profiles(),
                "provider_limits": {"claude_code": configured_concurrency()},
                "chats": [{"id": s.id, "name": getattr(s, "name", "") or s.id} for s in owned.values()
                          if not getattr(s, "archived", False)][:200]}

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
                if not frame.startswith("data: "):
                    yield frame
                    continue
                try:
                    ev = json.loads(frame[6:])
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
            return await agent_control.stop_run(run_id)
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

    def _active_archive_targets(owned: Dict[str, Any], session_id: str) -> list[str]:
        """Return target plus descendants that still have live work.

        A worker chat can be attached through ``parent_session``.  Archiving a
        parent must not make an active child vanish from the normal workspace,
        so the guard walks that owner-scoped relationship before changing
        anything.  It intentionally does not stop work; stopping remains an
        explicit action in the dashboard.
        """
        from core.database import get_session_settings
        descendants = {session_id}
        changed = True
        while changed:
            changed = False
            for sid in owned:
                if sid in descendants:
                    continue
                try:
                    parent = (get_session_settings(sid) or {}).get("parent_session")
                except Exception as exc:
                    # This is a safety check. Failing open could archive a
                    # parent while an unseen child is still queued.
                    raise HTTPException(409, "Could not verify child work; try again") from exc
                if parent in descendants:
                    descendants.add(sid)
                    changed = True
        return sorted(sid for sid in descendants
                      if agent_runs.is_busy(sid) or activity.has_active_run(sid)
                      or agent_control.live_children(sid) > 0)

    @router.post("/sessions/{session_id}/archive")
    async def archive_session(request: Request, session_id: str):
        user, owned = _owned(request)
        sess = owned.get(session_id)
        if sess is None:
            raise HTTPException(404, "Session not found")
        active = _active_archive_targets(owned, session_id)
        if active:
            raise HTTPException(409, "Stop active work before archiving: " + ", ".join(active))
        try:
            if hasattr(session_manager, "archive_session"):
                session_manager.archive_session(session_id)
            else:  # Small test/embedded managers still get recoverable semantics.
                sess.archived = True
        except Exception as exc:
            logger.warning("Could not archive agent session %s for %s: %s", session_id, user, exc)
            raise HTTPException(500, "Could not archive this chat")
        return {"ok": True, "session_id": session_id, "archived": True,
                "message": "Archived. Its chat and run history are preserved."}

    @router.post("/sessions/{session_id}/unarchive")
    async def unarchive_session(request: Request, session_id: str):
        user, owned = _owned(request)
        sess = owned.get(session_id)
        try:
            # Archived sessions may no longer be resident in the session
            # manager after a restart, so authorise against their DB owner.
            # Never use this as a cross-owner restore path.
            from core.database import SessionLocal, Session as DbSession
            db = SessionLocal()
            try:
                row = db.query(DbSession).filter(DbSession.id == session_id).first()
                if row is None or getattr(row, "owner", None) != user:
                    raise HTTPException(404, "Session not found")
                row.archived = False
                db.commit()
            finally:
                db.close()
            if sess is not None:
                sess.archived = False
            elif hasattr(session_manager, "_load_session_from_db"):
                session_manager._load_session_from_db(session_id)
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            logger.warning("Could not restore agent session %s for %s: %s", session_id, user, exc)
            raise HTTPException(500, "Could not restore this chat")
        return {"ok": True, "session_id": session_id, "archived": False}

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
            if rec.get("status") not in {"completed", "finished", "failed", "error", "cancelled", "stopped", "incomplete", "interrupted"}:
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
                     "max_parallel_workers", "allowed_mcp_servers", "private_vault_access")

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
                              "delegation_policy": p.get("delegation_policy", "explicit")}
                             for p in agent_profiles.load_profiles()]}

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
        else:
            patch = {key: None for key in _LOADOUT_KEYS}
        saved = update_session_settings(session_id, patch)
        if saved is None:
            raise HTTPException(500, "Could not save the chat's loadout")
        return {"ok": True, "agent_profile": saved.get("agent_profile")}

    @router.post("/launch")
    async def launch(request: Request):
        user, owned = _owned(request)
        body = await request.json()
        parent = str((body or {}).get("parent_session") or "").strip() or None
        if parent and parent not in owned:
            raise HTTPException(404, "Parent chat not found")
        try:
            return await agent_control.launch_worker(
                owner=user, task=str((body or {}).get("task") or ""),
                profile_name=str((body or {}).get("profile") or "").strip() or None,
                parent_session=parent, model=str((body or {}).get("model") or "").strip() or None,
                workspace=str((body or {}).get("workspace") or "").strip() or None,
            )
        except ValueError as exc:
            # A preflight refusal (WorkerBlocked) reads as the reason and the
            # fix, e.g. which checkout to pick.
            raise HTTPException(400, str(exc))

    return router
