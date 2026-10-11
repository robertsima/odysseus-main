"""Outcome routes: the person's own view of what they want closed.

Every route resolves the owner from the authenticated request and passes it to
`src.outcomes`, which filters on it; another user's id answers 404. Only
`POST /{id}/state` can change state or record the person's confirmation, and
only `POST /{id}/links` can link, so a PATCH body cannot reach either.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from src import outcomes as store
from src.auth_helpers import require_user
from src.owner_identity import effective_storage_owner

_STATUS = {"not_found": 404, "invalid": 400, "needs_waiting_on": 400, "needs_evidence": 409,
           "forbidden": 403, "error": 500}


class LinkIn(BaseModel):
    kind: str
    ref: str
    label: Optional[str] = None


class OutcomeCreate(BaseModel):
    title: str
    detail: Optional[str] = None
    kind: str = "obligation"
    state: str = "open"
    next_action: Optional[str] = None
    waiting_on: Optional[str] = None
    follow_up_at: Optional[str] = None
    due_at: Optional[str] = None
    priority: Optional[int] = None
    snoozed_until: Optional[str] = None
    links: List[LinkIn] = []


class OutcomePatch(BaseModel):
    """The fields a person edits in place. State, evidence and confirmation are absent on purpose."""
    title: Optional[str] = None
    detail: Optional[str] = None
    next_action: Optional[str] = None
    waiting_on: Optional[str] = None
    follow_up_at: Optional[str] = None
    due_at: Optional[str] = None
    priority: Optional[int] = None
    snoozed_until: Optional[str] = None


class StateIn(BaseModel):
    state: str
    confirm: bool = False
    waiting_on: Optional[str] = None
    follow_up_at: Optional[str] = None


def _owner(request: Request) -> str:
    owner = effective_storage_owner(require_user(request))
    if not owner:
        raise HTTPException(401, "Not authenticated")
    return owner


def _ok(result: Dict[str, Any]) -> Dict[str, Any]:
    if not result.get("ok"):
        raise HTTPException(_STATUS.get(result.get("code"), 400), result.get("error") or "Request failed")
    return result


def _full(owner: str, outcome_id: str) -> Dict[str, Any]:
    data = store.get(owner, outcome_id)
    if data is None:
        raise HTTPException(404, "Outcome not found")
    return {"outcome": data}


def setup_outcome_routes() -> APIRouter:
    router = APIRouter(prefix="/api/outcomes", tags=["outcomes"])

    @router.get("")
    async def list_outcomes(request: Request, state: Optional[str] = Query(None)) -> Dict[str, Any]:
        owner = _owner(request)
        states = [s.strip() for s in (state or "").split(",") if s.strip()] or None
        return {"outcomes": store.list_for(owner, states)}

    @router.post("")
    async def create_outcome(request: Request, body: OutcomeCreate) -> Dict[str, Any]:
        owner = _owner(request)
        data = body.model_dump()
        title, links = data.pop("title"), data.pop("links")
        return {"outcome": _ok(store.create(owner, title, links=links, source="person", actor="person", **data))["outcome"]}

    @router.get("/{outcome_id}")
    async def get_outcome(request: Request, outcome_id: str) -> Dict[str, Any]:
        return _full(_owner(request), outcome_id)

    @router.patch("/{outcome_id}")
    async def patch_outcome(request: Request, outcome_id: str, body: OutcomePatch) -> Dict[str, Any]:
        owner = _owner(request)
        _ok(store.update(owner, outcome_id, body.model_dump(exclude_unset=True), actor="person"))
        return _full(owner, outcome_id)

    @router.post("/{outcome_id}/state")
    async def set_outcome_state(request: Request, outcome_id: str, body: StateIn) -> Dict[str, Any]:
        owner = _owner(request)
        _ok(store.set_state(owner, outcome_id, body.state, actor="person", confirmed_by_person=body.confirm,
                            waiting_on=body.waiting_on, follow_up_at=body.follow_up_at))
        return _full(owner, outcome_id)

    @router.post("/{outcome_id}/links")
    async def add_outcome_link(request: Request, outcome_id: str, body: LinkIn) -> Dict[str, Any]:
        owner = _owner(request)
        result = _ok(store.add_link(owner, outcome_id, body.kind, body.ref, label=body.label, actor="person"))
        return {"link": result["link"]}

    @router.delete("/{outcome_id}/links/{link_id}")
    async def remove_outcome_link(request: Request, outcome_id: str, link_id: str) -> Dict[str, Any]:
        _ok(store.remove_link(_owner(request), outcome_id, link_id, actor="person"))
        return {"ok": True}

    @router.post("/{outcome_id}/blocker/clear")
    async def clear_outcome_blocker(request: Request, outcome_id: str) -> Dict[str, Any]:
        owner = _owner(request)
        _ok(store.set_blocker(owner, outcome_id, None, actor="person"))
        return _full(owner, outcome_id)

    return router
