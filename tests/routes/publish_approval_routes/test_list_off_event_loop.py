"""Publish-request routes read approval files; that must not happen on the event loop.

Production, 2026-10-23, app.log [loop-lag]: 1.35 s in ``approval._load`` <-
``list_requests`` <- GET /api/agent-publish/requests (an ``async def`` route
doing synchronous file reads on a NAS-backed data folder).
"""
import asyncio
import time

import httpx
import pytest
from fastapi import FastAPI

from routes import publish_approval_routes as mod

SLOW = 0.5
REQUESTS = {
    "r-alice": {"id": "r-alice", "status": "pending", "requested_by": "alice", "session_id": "s-a"},
    "r-bob": {"id": "r-bob", "status": "pending", "requested_by": "bob", "session_id": "s-b"},
}


class _Auth:
    def is_admin(self, user):
        return user == "admin"

    def get_privileges(self, user):
        return {}


@pytest.fixture
def client(monkeypatch):
    from src.agent_worktree import approval as approval_mod

    def slow_list(cfg=None):
        time.sleep(SLOW)
        return [dict(r) for r in REQUESTS.values()]

    def slow_get(rid, cfg=None):
        time.sleep(SLOW)
        if rid not in REQUESTS:
            raise approval_mod.ApprovalError("unknown")
        return dict(REQUESTS[rid])

    monkeypatch.setattr(approval_mod, "list_requests", slow_list)
    monkeypatch.setattr(approval_mod, "get_request", slow_get)
    monkeypatch.setattr(mod, "auth_disabled", lambda: False)
    monkeypatch.setattr("core.database.get_session_settings", lambda sid: {})

    app = FastAPI()
    app.state.auth_manager = _Auth()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        request.state.current_user = request.headers.get("x-test-user") or None
        request.state.api_token = False
        return await call_next(request)

    app.include_router(mod.setup_publish_approval_routes(None))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _with_heartbeat(call):
    started = time.monotonic()
    beat_at = []

    async def heartbeat():
        await asyncio.sleep(0.01)
        beat_at.append(time.monotonic() - started)

    beat = asyncio.create_task(heartbeat())
    resp = await call
    await beat
    assert beat_at and beat_at[0] < SLOW / 2, f"loop was blocked for {beat_at} s"
    return resp


@pytest.mark.security
async def test_listing_keeps_the_loop_responsive_and_shows_a_user_only_their_own(client):
    resp = await _with_heartbeat(client.get("/api/agent-publish/requests", headers={"x-test-user": "alice"}))
    assert [r["id"] for r in resp.json()["requests"]] == ["r-alice"]


@pytest.mark.security
async def test_reject_loads_the_request_off_the_loop_and_still_hides_other_users_requests(client):
    resp = await _with_heartbeat(
        client.post("/api/agent-publish/requests/r-bob/reject", headers={"x-test-user": "alice"}))
    assert resp.status_code == 404
