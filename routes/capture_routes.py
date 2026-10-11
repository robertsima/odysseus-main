"""Capture routes: one line in, routed to Todoist, the calendar, a note or an outcome.

The owner comes from the authenticated request, never from the body. A finished
capture is answered with 200 and a `status` (created, duplicate, needs_clarification
or failed); only a malformed request is a 4xx. Moving a capture answers 404 for a key
the caller never made, and 409 when the move is refused.
"""

from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src import capture
from src.auth_helpers import require_user
from src.owner_identity import effective_storage_owner

Destination = Literal["todoist", "calendar", "note", "outcome"]
_MOVE_STATUS = {"not_found": 404, "invalid": 400, "too_late": 409, "duplicate": 409, "not_ready": 409, "busy": 409}


class CaptureIn(BaseModel):
    text: str
    idempotency_key: Optional[str] = None
    destination: Optional[Destination] = None
    due: Optional[str] = None
    tz: Optional[str] = None
    tz_offset: Optional[int] = None


class MoveIn(BaseModel):
    to: Destination
    tz: Optional[str] = None
    tz_offset: Optional[int] = None


def _owner(request: Request) -> str:
    owner = effective_storage_owner(require_user(request))
    if not owner:
        raise HTTPException(401, "Not authenticated")
    return owner


def setup_capture_routes() -> APIRouter:
    router = APIRouter(prefix="/api/capture", tags=["capture"])

    @router.post("")
    async def post_capture(request: Request, body: CaptureIn) -> Dict[str, Any]:
        owner = _owner(request)
        result = await capture.capture(owner, body.text, idempotency_key=body.idempotency_key,
                                       destination=body.destination, due=body.due, actor="person",
                                       tz=body.tz, tz_offset=body.tz_offset)
        if result.get("code") == "invalid":
            raise HTTPException(400, result.get("error") or "Invalid capture")
        return result

    @router.post("/{key}/move")
    async def post_move(request: Request, key: str, body: MoveIn) -> Dict[str, Any]:
        owner = _owner(request)
        result = await capture.move(owner, key, body.to, actor="person", tz=body.tz, tz_offset=body.tz_offset)
        if result.get("status") == "refused":
            raise HTTPException(_MOVE_STATUS.get(result.get("code"), 409), result.get("error") or "Move refused")
        return result

    return router
