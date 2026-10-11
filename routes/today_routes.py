"""Today: what needs the person now, and the five things they can do about it.

The owner comes from the authenticated request. Todoist is one shared account,
so only an admin or the single local user reads it; everyone else gets Today
without it. The browser sends its time zone the way the chat does, so "today"
is the person's own day.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src import today
from src.auth_helpers import require_user
from src.owner_identity import effective_storage_owner
from src.user_time import clear_user_time_context, set_user_tz_name, set_user_tz_offset, user_timezone


class ActionIn(BaseModel):
    key: str
    action: str
    until: Optional[str] = None
    waiting_on: Optional[str] = None


def _is_admin_or_single_user(request: Request, user: Optional[str]) -> bool:
    if not user:
        # require_user admits an empty user only in the auth-off, loopback-bypass and first-run modes.
        return True
    try:
        from core.auth import AuthManager

        manager = getattr(request.app.state, "auth_manager", None) or AuthManager()
        return bool(not getattr(manager, "is_configured", True) or manager.is_admin(user))
    except Exception:
        return False


def _context(request: Request) -> today.Ctx:
    user = require_user(request)
    owner = effective_storage_owner(user)
    if not owner:
        raise HTTPException(401, "Not authenticated")
    clear_user_time_context()
    set_user_tz_offset(request.headers.get("x-tz-offset"))
    set_user_tz_name(request.headers.get("x-tz-name"))
    from datetime import timezone

    return today.Ctx(owner=owner, notes_owner=user or None, todoist=_is_admin_or_single_user(request, user),
                     now=today._utcnow().replace(tzinfo=timezone.utc), tz=user_timezone())


def setup_today_routes() -> APIRouter:
    router = APIRouter(prefix="/api/today", tags=["today"])

    @router.get("")
    async def get_today(request: Request) -> Dict[str, Any]:
        return await today.build(_context(request))

    @router.post("/action")
    async def post_action(request: Request, body: ActionIn) -> Dict[str, Any]:
        ctx = _context(request)
        try:
            return await today.perform(ctx, body.key, body.action, until=body.until, waiting_on=body.waiting_on)
        except today.ActionError as exc:
            raise HTTPException(exc.status, exc.message)

    return router
