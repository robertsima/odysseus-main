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
* ``GET  /approvals``           — pending tool approvals across the caller's chats.
* ``POST /launch``              — start a worker profile in a fresh chat.
"""

from __future__ import annotations

import json
import logging
import time
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

    @router.get("/overview")
    async def overview(request: Request, current_session: Optional[str] = Query(default=None)):
        user, owned = _owned(request)
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
        for rec in tool_approvals.pending_for(set(owned)):
            pending[rec["session_id"]] = pending.get(rec["session_id"], 0) + 1
        from core.database import get_session_settings

        rows = []
        visible_ids = set(chat_runs) | set(by_session) | set(pending)
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
            rows.append({
                "session_id": sid,
                "name": getattr(sess, "name", "") or sid,
                "model": getattr(sess, "model", ""),
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
                "steer_queued": len(agent_control.pending_steer(sid)),
                "profile": settings.get("agent_profile"),
                "parent_session": settings.get("parent_session"),
                "approval_mode": settings.get("approval_mode"),
                "is_current": sid == current_session,
                "config": {
                    "agent_profile": settings.get("agent_profile"),
                    "approval_mode": settings.get("approval_mode"),
                    "disabled_tools": settings.get("disabled_tools") or [],
                    "memory_access": settings.get("memory_access", "write"),
                    "skill_access": settings.get("skill_access", "all"),
                    "skill_names": settings.get("skill_names") or [],
                    "model_access": settings.get("model_access", "all"),
                    "allowed_models": settings.get("allowed_models") or [],
                    "delegation_policy": settings.get("delegation_policy", "explicit"),
                    "max_parallel_workers": settings.get("max_parallel_workers", 1),
                    "allowed_mcp_servers": settings.get("allowed_mcp_servers", ["*"]),
                    "private_vault_access": bool(settings.get("private_vault_access", False)),
                },
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

        return {"now": now, "rows": rows, "totals": totals, "profiles": agent_profiles.load_profiles(),
                "chats": [{"id": s.id, "name": getattr(s, "name", "") or s.id} for s in owned.values()
                          if not getattr(s, "archived", False)][:200]}

    @router.get("/catalog")
    async def catalog(request: Request):
        """Safe capability inventory used by the per-agent loadout editor."""
        user, owned = _owned(request)
        from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
        from src.tool_security import owner_baseline_disabled_tools, blocked_tools_for_owner

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
                "models": sorted({str(model) for model in models if model}, key=str.casefold)}

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
        if not agent_runs.is_busy(session_id):
            raise HTTPException(409, "That chat is not running; send it a normal message instead")
        # Only the agent loop drains the queue, and only between rounds. A plain
        # single-shot reply is "busy" but has no rounds, so accepting a steer for
        # one would swallow the message outright. 409 tells the caller to send it
        # as an ordinary turn instead.
        if not agent_control.is_steerable(session_id):
            raise HTTPException(409, "That chat is not running an agent turn; send it a normal message instead")
        try:
            rec = agent_control.steer(session_id, str((body or {}).get("text") or ""), owner=user)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"queued": True, "text": rec["text"], "pending": len(agent_control.pending_steer(session_id))}

    @router.get("/approvals")
    async def approvals(request: Request):
        user, owned = _owned(request)
        rows = []
        for rec in tool_approvals.pending_for(set(owned)):
            sess = owned.get(rec["session_id"])
            rows.append({**rec, "session_name": getattr(sess, "name", "") if sess else ""})
        return {"approvals": rows}

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
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    return router
