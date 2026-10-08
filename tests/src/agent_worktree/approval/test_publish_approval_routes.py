"""Approving an agent's publish request in the browser.

Before 2026-09-29 a person could only approve on the host (the operator CLI)
and paste a one-time code back to the agent. Now an admin, or a user with the
can_approve_publish privilege for requests from their own chats, approves in
the UI and the server publishes at once. The agent must never be able to take
that decision itself: its loopback calls (the internal tool header, which can
carry a real owner's name), API tokens and cross-site requests are refused.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.middleware import INTERNAL_TOOL_HEADER
from routes import publish_approval_routes as mod

REQUESTS = {
    "r-alice": {"id": "r-alice", "status": "pending", "requested_by": "alice", "session_id": "worker-a",
                "repo": "o/umni", "branch": "agent/umni/x", "head_sha": "a" * 40, "title": "Alice change",
                "changed_files": ["a.py"], "sensitive": {}},
    "r-bob": {"id": "r-bob", "status": "pending", "requested_by": "bob", "session_id": "chat-b",
              "repo": "o/umni", "branch": "agent/umni/y", "head_sha": "b" * 40, "title": "Bob change",
              "changed_files": ["ci.yml"], "sensitive": {"ci": [".github/workflows/ci.yml"]}},
    "r-done": {"id": "r-done", "status": "used", "requested_by": "alice", "session_id": "chat-a"},
}


class FakeAuth:
    users = {"admin": {"is_admin": True}, "alice": {}, "bob": {}, "carol": {}}
    privileged = {"alice", "carol"}

    def is_admin(self, user):
        return user == "admin"

    def get_privileges(self, user):
        return {"can_approve_publish": user in self.privileged}


class FakeSessions:
    def __init__(self):
        self.added = []

    def add_message(self, sid, message):
        self.added.append((sid, message.content, message.metadata))


@pytest.fixture
def env(monkeypatch):
    from src.agent_worktree import approval as approval_mod
    from src.agent_worktree import service

    calls = {"approve": [], "revoke": []}
    monkeypatch.setattr(approval_mod, "list_requests", lambda cfg=None: [dict(r) for r in REQUESTS.values()])

    def get_request(rid, cfg=None):
        if rid not in REQUESTS:
            raise approval_mod.ApprovalError("unknown")
        return dict(REQUESTS[rid])

    monkeypatch.setattr(approval_mod, "get_request", get_request)
    monkeypatch.setattr(approval_mod, "revoke",
                        lambda rid, cfg=None: calls["revoke"].append(rid) or {**REQUESTS[rid], "status": "revoked"})

    async def approve_and_publish(rid, *, approver, allow_sensitive=False, cfg=None):
        calls["approve"].append((rid, approver, allow_sensitive))
        return {"branch": REQUESTS[rid]["branch"], "head_sha": REQUESTS[rid]["head_sha"],
                "pull_request": {"html_url": "https://github.com/o/umni/pull/7"}}

    monkeypatch.setattr(service, "approve_and_publish", approve_and_publish)
    parents = {"worker-a": "chat-a"}
    monkeypatch.setattr("core.database.get_session_settings",
                        lambda sid: {"parent_session": parents.get(sid)} if sid in parents else {})
    monkeypatch.setattr(mod, "auth_disabled", lambda: False)

    sessions = FakeSessions()
    app = FastAPI()
    app.state.auth_manager = FakeAuth()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        # Stands in for the app's AuthMiddleware: a cookie session names the
        # user; a bearer token marks api_token.
        request.state.current_user = request.headers.get("x-test-user") or None
        request.state.api_token = request.headers.get("x-test-token") == "1"
        return await call_next(request)

    app.include_router(mod.setup_publish_approval_routes(sessions))
    client = TestClient(app)
    return client, calls, sessions


def _as(user, **extra):
    return {"x-test-user": user, **extra}


def test_admin_sees_every_open_request_and_users_only_their_own(env):
    client, _, _ = env
    ids = lambda user: sorted(r["id"] for r in client.get("/api/agent-publish/requests", headers=_as(user)).json()["requests"])
    assert ids("admin") == ["r-alice", "r-bob"]         # open ones; r-done is used
    assert ids("alice") == ["r-alice"]
    assert ids("carol") == []


def test_a_chat_shows_its_workers_requests(env):
    client, _, _ = env
    rows = client.get("/api/agent-publish/requests?session_id=chat-a", headers=_as("alice")).json()["requests"]
    assert [r["id"] for r in rows] == ["r-alice"]        # asked in worker-a, a worker of chat-a
    assert rows[0]["can_decide"] is True


def test_a_privileged_user_approves_their_own_request_and_the_chat_is_told(env):
    client, calls, sessions = env
    resp = client.post("/api/agent-publish/requests/r-alice/approve", json={}, headers=_as("alice"))
    assert resp.status_code == 200, resp.text
    assert calls["approve"] == [("r-alice", "alice", False)]
    assert sessions.added and sessions.added[0][0] == "worker-a"
    assert "pull/7" in sessions.added[0][1]


def test_admin_approves_anyones_request_after_confirming_sensitive_paths(env):
    client, calls, _ = env
    assert client.post("/api/agent-publish/requests/r-bob/approve", json={},
                       headers=_as("admin")).status_code == 400
    assert client.post("/api/agent-publish/requests/r-bob/approve", json={"allow_sensitive": True},
                       headers=_as("admin")).status_code == 200
    assert calls["approve"] == [("r-bob", "admin", True)]


@pytest.mark.parametrize("headers,status", [
    ({INTERNAL_TOOL_HEADER: "anything", "x-test-user": "alice"}, 403),   # the agent's loopback, as alice
    ({"x-test-user": "alice", "x-test-token": "1"}, 403),                # an API token
    ({}, 401),                                                            # nobody logged in
    ({"x-test-user": "alice", "origin": "https://evil.example"}, 403),   # another site
    ({"x-test-user": "alice", "sec-fetch-site": "cross-site"}, 403),
])
def test_only_a_person_in_this_app_can_approve(env, headers, status):
    client, calls, _ = env
    resp = client.post("/api/agent-publish/requests/r-alice/approve", json={}, headers=headers)
    assert resp.status_code == status, resp.text
    assert calls["approve"] == []


def test_without_the_privilege_or_for_someone_else_it_is_refused(env):
    client, calls, _ = env
    assert client.post("/api/agent-publish/requests/r-bob/approve", json={"allow_sensitive": True},
                       headers=_as("bob")).status_code == 403            # own request, no privilege
    assert client.post("/api/agent-publish/requests/r-bob/approve", json={"allow_sensitive": True},
                       headers=_as("carol")).status_code == 404          # privileged, not hers
    assert calls["approve"] == []


def test_a_failed_publish_is_reported(env, monkeypatch):
    client, _, _ = env
    from src.agent_worktree import service

    async def failing(rid, **_k):
        raise service.WorktreeError("publishing is not available: no GitHub credential configured")

    monkeypatch.setattr(service, "approve_and_publish", failing)
    resp = client.post("/api/agent-publish/requests/r-alice/approve", json={}, headers=_as("alice"))
    assert resp.status_code == 409 and "no GitHub credential" in resp.json()["detail"]


def test_reject_revokes_and_tells_the_chat(env):
    client, calls, sessions = env
    assert client.post("/api/agent-publish/requests/r-alice/reject", json={}, headers=_as("alice")).status_code == 200
    assert calls["revoke"] == ["r-alice"]
    assert "rejected" in sessions.added[0][1].lower()


def test_requests_remember_the_chat_and_the_privilege_is_off_by_default():
    from core import auth as auth_mod
    from src.agent_worktree import approval as approval_mod

    view = approval_mod.public_view({"id": "x", "session_id": "chat-1", "body": "why"})
    assert view["session_id"] == "chat-1" and view["body"] == "why"
    assert auth_mod.DEFAULT_PRIVILEGES["can_approve_publish"] is False
    assert auth_mod.ADMIN_PRIVILEGES["can_approve_publish"] is True


def test_the_agent_is_told_a_person_approves_in_the_ui():
    from src import tool_schemas

    text = str(tool_schemas.__dict__)
    assert "approves it in the Agamemnon UI" in text
    assert "paste the code back to you" not in text


# ── service.approve_and_publish: the code goes straight to publish ─────────

def test_approve_and_publish_grants_then_publishes_with_that_code(monkeypatch):
    import asyncio

    from src.agent_worktree import approval as approval_mod
    from src.agent_worktree import service

    seen = {}
    monkeypatch.setattr(approval_mod, "get_request", lambda rid, cfg=None: {"id": rid, "repository": "/repo"})

    async def repo_cfg(cfg, repository):
        return object()

    monkeypatch.setattr(service, "repository_config", repo_cfg)
    monkeypatch.setattr(service, "publish_blockers", lambda target: [])
    monkeypatch.setattr(approval_mod, "grant",
                        lambda rid, allow_sensitive=False, granted_by=None, cfg=None:
                        seen.update(granted_by=granted_by, sensitive=allow_sensitive) or ("CODE-1", {}))

    async def publish(rid, code, cfg=None):
        seen["code"] = code
        return {"branch": "agent/umni/x"}

    monkeypatch.setattr(service, "publish", publish)
    out = asyncio.run(service.approve_and_publish("r1", approver="alice", allow_sensitive=True, cfg=object()))
    assert out == {"branch": "agent/umni/x"}
    assert seen == {"granted_by": "alice", "sensitive": True, "code": "CODE-1"}


def test_approve_and_publish_grants_nothing_when_publishing_is_not_set_up(monkeypatch):
    import asyncio

    from src.agent_worktree import approval as approval_mod
    from src.agent_worktree import service

    monkeypatch.setattr(approval_mod, "get_request", lambda rid, cfg=None: {"id": rid, "repository": None})

    async def repo_cfg(cfg, repository):
        return object()

    monkeypatch.setattr(service, "repository_config", repo_cfg)
    monkeypatch.setattr(service, "publish_blockers", lambda target: ["publishing is disabled"])
    monkeypatch.setattr(approval_mod, "grant", lambda *a, **k: pytest.fail("granted despite blockers"))
    with pytest.raises(service.WorktreeError, match="publishing is disabled"):
        asyncio.run(service.approve_and_publish("r1", approver="alice", cfg=object()))
