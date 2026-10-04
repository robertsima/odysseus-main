"""Pin the auth-gate fixes from the 2026-05-19 v2 review so they
don't regress. Specifically:

- All `/api/research/*` endpoints reject anonymous callers.
- Task `create_task` blocks shell-executing action types for
  non-admins (`run_local`, `run_script`, `ssh_command`).
- `pop_notifications(owner)` returns only the calling user's
  notifications; ownerless legacy notifications are drained only by
  anonymous/no-owner callers.
"""

import asyncio
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import HTTPException

# ---------------------------------------------------------------------------
# Auth routes -- open signup setter
# ---------------------------------------------------------------------------

def _auth_route_endpoint(path: str, method: str):
    from routes.auth_routes import setup_auth_routes

    auth_manager = MagicMock()
    router = setup_auth_routes(auth_manager)
    for route in router.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return auth_manager, route.endpoint
    raise AssertionError(f"{method} {path} route not registered")


def _fake_auth_request(token="session-token"):
    from routes.auth_routes import SESSION_COOKIE

    req = SimpleNamespace()
    req.cookies = {SESSION_COOKIE: token}
    req.client = SimpleNamespace(host="127.0.0.1")
    return req


def test_set_signup_enabled_true_is_idempotent():
    from routes.auth_routes import SetOpenRegistrationRequest

    auth, target = _auth_route_endpoint("/api/auth/open-signup", "PUT")
    auth.get_username_for_token.return_value = "admin"
    auth.is_admin.return_value = True

    request = _fake_auth_request()
    auth.signup_enabled = False

    out = asyncio.run(target(body=SetOpenRegistrationRequest(enabled=True),request=request))

    assert out == {"ok": True, "signup_enabled": True}
    assert auth.signup_enabled is True

    out = asyncio.run(target(body=SetOpenRegistrationRequest(enabled=True), request=request))

    assert out == {"ok": True, "signup_enabled": True}
    assert auth.signup_enabled is True

def test_set_signup_enabled_false_is_idempotent():
    from routes.auth_routes import SetOpenRegistrationRequest

    auth, target = _auth_route_endpoint("/api/auth/open-signup", "PUT")
    auth.get_username_for_token.return_value = "admin"
    auth.is_admin.return_value = True

    request = _fake_auth_request()
    auth.signup_enabled = True

    out = asyncio.run(target(body=SetOpenRegistrationRequest(enabled=False), request=request))

    assert out == {"ok": True, "signup_enabled": False}
    assert auth.signup_enabled is False

    out = asyncio.run(target(body=SetOpenRegistrationRequest(enabled=False), request=request))

    assert out == {"ok": True, "signup_enabled": False}
    assert auth.signup_enabled is False

def test_set_signup_enabled_requires_admin():
    from routes.auth_routes import SetOpenRegistrationRequest

    auth, target = _auth_route_endpoint("/api/auth/open-signup", "PUT")
    auth.get_username_for_token.return_value = "bob"
    auth.is_admin.return_value = False
    auth.signup_enabled = False

    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(body=SetOpenRegistrationRequest(enabled=True), request=_fake_auth_request()))

    assert exc.value.status_code == 403
    assert auth.signup_enabled is False

# ---------------------------------------------------------------------------
# Research endpoints — `_require_user` rejects anonymous
# ---------------------------------------------------------------------------

def _build_research_router():
    """Construct the research router with a mock research_handler so we
    can fish out the inner `_require_user` helper without booting the
    full app."""
    from routes.research_routes import setup_research_routes
    rh = MagicMock()
    setup_research_routes(rh)
    # The helper lives inside the setup closure. Easiest way to exercise
    # it: re-import the module and grab the symbol via its source.
    # Instead, exercise it via the route helper that has request:Request.
    return rh


def _fake_request(user=None):
    """Cheap stand-in for fastapi.Request — only `request.state.current_user`
    matters to `get_current_user`."""
    req = SimpleNamespace()
    req.state = SimpleNamespace(current_user=user)
    # Some endpoints touch .client too — provide a benign default.
    req.client = SimpleNamespace(host="127.0.0.1")
    return req


def test_research_status_rejects_anonymous():
    """research_status must 401 when no user is on the request state."""
    # Build a fresh router and pluck its registered routes.
    from routes.research_routes import setup_research_routes
    rh = MagicMock()
    rh.get_status.return_value = {"status": "running"}  # would 200 if auth passed
    router = setup_research_routes(rh)
    # Find the function registered for /api/research/status/{session_id}
    target = None
    for route in router.routes:
        if getattr(route, "path", "") == "/api/research/status/{session_id}":
            target = route.endpoint
            break
    assert target is not None, "research_status route not registered"
    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(session_id="x", request=_fake_request(user=None)))
    assert exc.value.status_code == 401


def test_research_status_accepts_authenticated():
    from routes.research_routes import setup_research_routes
    rh = MagicMock()
    rh._active_tasks = {"x": {"owner": "alice", "status": "running"}}
    rh.get_status.return_value = {"status": "running", "progress": {}}
    router = setup_research_routes(rh)
    target = next(r.endpoint for r in router.routes if getattr(r, "path", "") == "/api/research/status/{session_id}")
    out = asyncio.run(target(session_id="x", request=_fake_request(user="alice")))
    assert out == {"status": "running", "progress": {}}


def test_research_status_rejects_wrong_owner():
    from routes.research_routes import setup_research_routes
    rh = MagicMock()
    rh._active_tasks = {"x": {"owner": "alice", "status": "running"}}
    rh.get_status.return_value = {"status": "running", "progress": {}}
    router = setup_research_routes(rh)
    target = next(r.endpoint for r in router.routes if getattr(r, "path", "") == "/api/research/status/{session_id}")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(session_id="x", request=_fake_request(user="bob")))
    assert exc.value.status_code == 404


def test_research_cancel_rejects_anonymous():
    from routes.research_routes import setup_research_routes
    rh = MagicMock()
    router = setup_research_routes(rh)
    target = next(r.endpoint for r in router.routes if getattr(r, "path", "") == "/api/research/cancel/{session_id}")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(session_id="x", request=_fake_request(user=None)))
    assert exc.value.status_code == 401


def test_research_delete_rejects_anonymous():
    from routes.research_routes import setup_research_routes
    rh = MagicMock()
    router = setup_research_routes(rh)
    target = next(r.endpoint for r in router.routes if getattr(r, "path", "") == "/api/research/{session_id}")
    # Note: `target` here is the most-recently registered route on this
    # path which is the DELETE. Either /detail or /delete both match
    # other paths — the {session_id} bare path is DELETE.
    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(session_id="x", request=_fake_request(user=None)))
    assert exc.value.status_code == 401


def test_research_spinoff_rejects_anonymous():
    """spinoff must 401 before reading any research data."""
    from routes.research_routes import setup_research_routes
    rh = MagicMock()
    router = setup_research_routes(rh, session_manager=MagicMock())
    target = next(r.endpoint for r in router.routes if getattr(r, "path", "") == "/api/research/spinoff/{session_id}")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(session_id="x", request=_fake_request(user=None)))
    assert exc.value.status_code == 401


def test_research_spinoff_rejects_wrong_owner():
    """A user must not be able to spin off (and thereby read) another user's
    research report. The ownership gate must 404 before any data is read or a
    new session is created. Regression for the cross-user disclosure IDOR."""
    from routes.research_routes import setup_research_routes
    sm = MagicMock()
    rh = MagicMock()
    rh._active_tasks = {"x": {"owner": "alice"}}
    rh.get_result.return_value = "TOP SECRET REPORT"
    router = setup_research_routes(rh, session_manager=sm)
    target = next(r.endpoint for r in router.routes if getattr(r, "path", "") == "/api/research/spinoff/{session_id}")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(session_id="x", request=_fake_request(user="bob")))
    assert exc.value.status_code == 404
    # The attacker must never get a session created on their behalf.
    sm.create_session.assert_not_called()


# ---------------------------------------------------------------------------
# pop_notifications owner filter
# ---------------------------------------------------------------------------

def test_pop_notifications_owner_filtered():
    """pop_notifications(owner='alice') must return only alice's items.
    bob's and legacy ownerless items stay behind in the queue."""
    # Build a minimal scheduler instance that we can hit directly.
    # Reuse the real class so the test catches future regressions of
    # the filter logic.
    from src.task_scheduler import TaskScheduler
    sch = TaskScheduler.__new__(TaskScheduler)  # bypass __init__ network etc.
    sch._pending_notifications = []
    sch.add_notification("t1", "success", "id1", owner="alice")
    sch.add_notification("t2", "error",   "id2", owner="bob")
    sch.add_notification("t3", "success", "id3", owner=None)
    sch.add_notification("t4", "success", "id4", owner="alice")
    alice = sch.pop_notifications(owner="alice")
    alice_names = {n["task_name"] for n in alice}
    # alice gets only her own rows; bob's row and legacy null-owner rows stay.
    assert alice_names == {"t1", "t4"}
    # bob's row and the legacy ownerless row are still queued.
    remaining = sch._pending_notifications
    assert {n["task_name"] for n in remaining} == {"t2", "t3"}
    # Anonymous caller (owner=None) drains everything that's left.
    rest = sch.pop_notifications(owner=None)
    assert {n["task_name"] for n in rest} == {"t2", "t3"}
    assert sch._pending_notifications == []


# ---------------------------------------------------------------------------
# Task action allowlist
# ---------------------------------------------------------------------------

def test_admin_only_actions_set_contains_shell_runners():
    """The constant defining shell-executing action types must include
    the three risky entries. Catches accidental removal."""
    from src.task_action_policy import ADMIN_ONLY_TASK_ACTIONS

    assert "run_local" in ADMIN_ONLY_TASK_ACTIONS
    assert "run_script" in ADMIN_ONLY_TASK_ACTIONS
    assert "ssh_command" in ADMIN_ONLY_TASK_ACTIONS


def test_task_create_notification_default_allows_action_specific_defaults():
    """Omitted notifications_enabled should stay None so create_task can
    default noisy/quiet built-ins differently."""
    from routes.task_routes import TaskCreate

    req = TaskCreate(task_type="action", action="check_email_urgency", schedule="cron", cron_expression="*/15 * * * *")
    assert req.notifications_enabled is None
