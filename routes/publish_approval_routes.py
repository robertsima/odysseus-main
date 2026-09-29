"""Approve an agent's publish request in the Odysseus UI.

An agent can freeze a change and ask to publish it (manage_agent_worktree
request_publish); a person decides. Until 2026-09-29 the only way to decide
was the operator CLI on the host (scripts/odysseus-agent-worktree approve),
then pasting its one-time code back to the agent. Here the person approves in
the browser and the server publishes at once, so the code never exists
anywhere the agent can read.

Who may decide: an admin, any request; a user with the ``can_approve_publish``
privilege, requests from their own chats. Deciding is only possible from a
logged-in browser session: the agent's loopback calls (the internal tool
header, which can carry a real owner's name), bearer API tokens and
cross-site requests are refused.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request

from core.middleware import INTERNAL_TOOL_HEADER
from src.owner_identity import INTERNAL_TOOL_USER, auth_disabled

logger = logging.getLogger(__name__)


def _auth(request: Request):
    return getattr(request.app.state, "auth_manager", None)


def _user(request: Request) -> Optional[str]:
    """The logged-in person, refusing anything that is not one."""
    if request.headers.get(INTERNAL_TOOL_HEADER) is not None:
        raise HTTPException(403, "Agents cannot see or decide publish requests here; a person approves in the UI")
    if getattr(request.state, "current_user", None) == INTERNAL_TOOL_USER:
        raise HTTPException(403, "Agents cannot see or decide publish requests here")
    if getattr(request.state, "api_token", False):
        raise HTTPException(403, "Publish requests are decided in the Odysseus UI, not with an API token")
    user = getattr(request.state, "current_user", None)
    if not user and not auth_disabled():
        raise HTTPException(401, "Log in to review publish requests")
    return user


def _same_origin(request: Request) -> None:
    """A decision must come from this app's own pages."""
    site = (request.headers.get("sec-fetch-site") or "").lower()
    if site and site not in ("same-origin", "none"):
        raise HTTPException(403, "Cross-site request refused")
    origin = request.headers.get("origin")
    if origin:
        host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
        if urlparse(origin).netloc.lower() != host.lower():
            raise HTTPException(403, "Cross-origin request refused")


def _is_admin(request: Request, user: Optional[str]) -> bool:
    if auth_disabled():
        return True
    mgr = _auth(request)
    return bool(mgr and user and mgr.is_admin(user))


def _can_see(request: Request, user: Optional[str], record: Dict[str, Any]) -> bool:
    return _is_admin(request, user) or (bool(user) and record.get("requested_by") == user)


def _can_decide(request: Request, user: Optional[str], record: Dict[str, Any]) -> bool:
    if _is_admin(request, user):
        return True
    mgr = _auth(request)
    privileges = mgr.get_privileges(user) if mgr and user else {}
    return bool(privileges.get("can_approve_publish")) and record.get("requested_by") == user


def _lineage(session_id: Optional[str], limit: int = 4) -> List[str]:
    """The chat and the chats above it (a worker's parent, its parent...)."""
    chain: List[str] = []
    current = session_id
    try:
        from core.database import get_session_settings
    except Exception:
        return [session_id] if session_id else []
    while current and current not in chain and len(chain) < limit:
        chain.append(current)
        try:
            current = (get_session_settings(current) or {}).get("parent_session")
        except Exception:
            break
    return chain


def _load(request_id: str) -> Dict[str, Any]:
    from src.agent_worktree import approval as approval_mod

    try:
        return approval_mod.get_request(request_id)
    except approval_mod.ApprovalError:
        raise HTTPException(404, "No such publish request")


def setup_publish_approval_routes(session_manager=None) -> APIRouter:
    router = APIRouter(prefix="/api/agent-publish", tags=["agent-publish"])

    def _note_in_chat(record: Dict[str, Any], text: str, user: Optional[str]) -> None:
        """Record the decision in the chat that asked, so its agent knows."""
        sid = record.get("session_id")
        if not sid or session_manager is None:
            return
        try:
            from core.models import ChatMessage

            session_manager.add_message(sid, ChatMessage("user", text, {
                "source": "publish_decision", "request_id": record.get("id"), "decided_by": user}))
        except Exception:
            logger.debug("publish decision: could not note it in chat %s", sid, exc_info=True)

    @router.get("/requests")
    async def list_requests(request: Request, session_id: str = "", status: str = "open") -> Dict[str, Any]:
        user = _user(request)
        from src.agent_worktree import approval as approval_mod

        rows = []
        for rec in approval_mod.list_requests():
            if not _can_see(request, user, rec):
                continue
            if status == "open" and rec.get("status") not in ("pending", "granted"):
                continue
            if session_id and session_id not in _lineage(rec.get("session_id")):
                continue
            rows.append({**rec, "can_decide": _can_decide(request, user, rec)})
        return {"requests": rows}

    @router.get("/requests/{request_id}")
    async def show_request(request: Request, request_id: str) -> Dict[str, Any]:
        user = _user(request)
        rec = _load(request_id)
        if not _can_see(request, user, rec):
            raise HTTPException(404, "No such publish request")
        from src.agent_worktree import service
        from src.agent_worktree.config import load_config, publish_blockers

        out: Dict[str, Any] = {**rec, "can_decide": _can_decide(request, user, rec)}
        try:
            target = await service.repository_config(load_config(), rec.get("repository"))
            out["blockers"] = publish_blockers(target)
        except Exception as exc:  # noqa: BLE001 - shown to the reviewer, not fatal
            out["blockers"] = [str(exc)]
        try:
            out["review"] = await service.review_patch(request_id)
        except Exception as exc:  # noqa: BLE001
            out["review"] = {"error": str(exc)}
        return out

    @router.post("/requests/{request_id}/approve")
    async def approve(request: Request, request_id: str) -> Dict[str, Any]:
        user = _user(request)
        _same_origin(request)
        rec = _load(request_id)
        if not _can_see(request, user, rec):
            raise HTTPException(404, "No such publish request")
        if not _can_decide(request, user, rec):
            raise HTTPException(403, "You cannot approve this request (ask an admin for the approve privilege)")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Send a JSON body")
        allow_sensitive = bool((body or {}).get("allow_sensitive"))
        if rec.get("sensitive") and not allow_sensitive:
            raise HTTPException(400, "This change touches sensitive paths; confirm you reviewed them")
        from src.agent_worktree import service

        try:
            result = await service.approve_and_publish(request_id, approver=user or "local",
                                                       allow_sensitive=allow_sensitive)
        except service.WorktreeError as exc:
            logger.info("publish approval failed id=%s by=%s: %s", request_id, user, exc)
            raise HTTPException(409, str(exc))
        pr = result.get("pull_request") or {}
        url = str(pr.get("html_url") or pr.get("url") or "")
        logger.info("publish approved in the UI id=%s by=%s branch=%s", request_id, user, result.get("branch"))
        _note_in_chat(rec, f"[Publish approved by {user or 'the local user'}] Pushed {result.get('branch')} "
                           f"@ {str(result.get('head_sha') or '')[:12]}"
                           + (f"; draft PR: {url}" if url else "") + ".", user)
        # Publishing is a step in the chat's request, not its end: the chat
        # that asked carries on (checks the PR, reports) once it is idle.
        try:
            from src import agent_control

            agent_control.schedule_publish_followup(session_manager, rec.get("session_id"), None, rec.get("id"))
        except Exception:
            logger.debug("publish approval: could not schedule the chat's follow-up", exc_info=True)
        return {"ok": True, "published": result}

    @router.post("/requests/{request_id}/reject")
    async def reject(request: Request, request_id: str) -> Dict[str, Any]:
        user = _user(request)
        _same_origin(request)
        rec = _load(request_id)
        if not _can_see(request, user, rec):
            raise HTTPException(404, "No such publish request")
        if not _can_decide(request, user, rec):
            raise HTTPException(403, "You cannot decide this request")
        from src.agent_worktree import approval as approval_mod

        try:
            view = approval_mod.revoke(request_id)
        except approval_mod.ApprovalError as exc:
            raise HTTPException(409, str(exc))
        logger.info("publish rejected in the UI id=%s by=%s", request_id, user)
        _note_in_chat(rec, f"[Publish rejected by {user or 'the local user'}] {rec.get('branch')} was not pushed.",
                      user)
        return {"ok": True, "request": view}

    return router
