"""Pin the null-owner-bypass fixes so they don't regress.

The same legacy `if row.owner and row.owner != user` / `(owner == user) |
(owner == None)` pattern has regressed THREE times across reviews —
once in gallery, once in calendar, once in notes/daily-brief. Without
tests it'll keep coming back. These tests exercise the small helper
functions directly against MagicMock'd model rows.

Pattern under test (multi-tenant deploy):
  user "alice" must NOT be able to read/write a row whose owner is None
  or whose owner is "bob".
"""

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from tests.helpers.calendar_routes import import_calendar_routes

from fastapi import HTTPException


# ---------------------------------------------------------------------------
# calendar._get_or_404_calendar / _get_or_404_event
# ---------------------------------------------------------------------------

def test_calendar_gate_rejects_null_owner_for_authenticated_user():
    cal_mod = import_calendar_routes()
    db = MagicMock()
    cal = SimpleNamespace(id="c1", owner=None)
    db.query.return_value.filter.return_value.first.return_value = cal
    with pytest.raises(HTTPException) as exc:
        cal_mod._get_or_404_calendar(db, "c1", owner="alice")
    assert exc.value.status_code == 404


def test_calendar_gate_rejects_cross_owner():
    cal_mod = import_calendar_routes()
    db = MagicMock()
    cal = SimpleNamespace(id="c1", owner="bob")
    db.query.return_value.filter.return_value.first.return_value = cal
    with pytest.raises(HTTPException) as exc:
        cal_mod._get_or_404_calendar(db, "c1", owner="alice")
    assert exc.value.status_code == 404


def test_calendar_gate_accepts_matching_owner():
    cal_mod = import_calendar_routes()
    db = MagicMock()
    cal = SimpleNamespace(id="c1", owner="alice")
    db.query.return_value.filter.return_value.first.return_value = cal
    out = cal_mod._get_or_404_calendar(db, "c1", owner="alice")
    assert out is cal


def test_calendar_event_gate_rejects_null_owner_calendar():
    cal_mod = import_calendar_routes()
    db = MagicMock()
    cal = SimpleNamespace(owner=None)
    ev = SimpleNamespace(uid="e1", calendar=cal)
    db.query.return_value.join.return_value.filter.return_value.first.return_value = ev
    with pytest.raises(HTTPException) as exc:
        cal_mod._get_or_404_event(db, "e1", owner="alice")
    assert exc.value.status_code == 404


def test_calendar_event_gate_rejects_cross_owner():
    cal_mod = import_calendar_routes()
    db = MagicMock()
    cal = SimpleNamespace(owner="bob")
    ev = SimpleNamespace(uid="e1", calendar=cal)
    db.query.return_value.join.return_value.filter.return_value.first.return_value = ev
    with pytest.raises(HTTPException) as exc:
        cal_mod._get_or_404_event(db, "e1", owner="alice")
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# document._owner_session_filter
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# gallery._owner_filter
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# webhook._caller_owns_session  (POST /api/v1/chat sync-chat endpoint)
# ---------------------------------------------------------------------------
# This is the FOURTH place the `owner and owner != user` pattern showed up:
# the token-authenticated sync-chat endpoint let any chat-scoped token resume
# a null-owner session by passing its id, leaking its history and reusing the
# owner's endpoint credentials. The gate must fail closed, exactly like the
# calendar/notes/gallery gates above and _verify_session_owner.

def _import_webhook_helper():
    """Import routes.webhook_routes."""
    return __import__(
        "routes.webhook_routes", fromlist=["_caller_owns_session"]
    )


def test_sync_chat_gate_rejects_null_owner_session():
    wh_mod = _import_webhook_helper()
    # Legacy/migrated session with no owner must NOT be resumable by a token.
    assert wh_mod._caller_owns_session(None, "alice") is False


def test_sync_chat_gate_rejects_cross_owner_session():
    wh_mod = _import_webhook_helper()
    assert wh_mod._caller_owns_session("bob", "alice") is False


def test_sync_chat_gate_rejects_unresolvable_caller():
    wh_mod = _import_webhook_helper()
    # If the token's owner can't be resolved, fail closed rather than opening
    # up null-owner sessions.
    assert wh_mod._caller_owns_session(None, None) is False
    assert wh_mod._caller_owns_session("alice", None) is False


def test_sync_chat_gate_accepts_matching_owner():
    wh_mod = _import_webhook_helper()
    assert wh_mod._caller_owns_session("alice", "alice") is True


# ---------------------------------------------------------------------------
# webhook._first_enabled_endpoint  (POST /api/v1/chat, Case 3 fallback)
# ---------------------------------------------------------------------------
# The SAME multi-tenant leak in a second spot on this endpoint: when a
# chat-scoped token sends no session and no api_key, sync-chat falls back to a
# configured ModelEndpoint and uses that row's *decrypted* api_key. The query
# was an unscoped `.first()`, so a token for "alice" could fall back onto
# "bob"'s PRIVATE endpoint and silently spend bob's API key / reach bob's
# internal base_url. The fallback must be owner-scoped (own rows + legacy
# null-owner shared rows), exactly like routes/model_routes.py and
# companion/routes.py.

class _Predicate:
    def __init__(self, check):
        self._check = check

    def __call__(self, row):
        return self._check(row)

    def __or__(self, other):
        return _Predicate(lambda row: self(row) or other(row))


class _Column:
    def __init__(self, name):
        self.name = name

    def __eq__(self, value):
        return _Predicate(lambda row: getattr(row, self.name) == value)

    def desc(self):
        return self


class _ModelEndpoint:
    is_enabled = _Column("is_enabled")
    owner = _Column("owner")
    created_at = _Column("created_at")


class _Query:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *predicates):
        self._rows = [r for r in self._rows if all(p(r) for p in predicates)]
        return self

    def order_by(self, *exprs):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class _DB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, model):
        assert model is _ModelEndpoint
        return _Query(self._rows)


def _ep(name, owner, *, is_enabled=True):
    return SimpleNamespace(name=name, owner=owner, is_enabled=is_enabled)


def _select(monkeypatch, rows, owner):
    wh_mod = _import_webhook_helper()
    # _select_api_chat_fallback_endpoint uses the module-level ModelEndpoint
    # (not a local import), so the fake class goes on the module attribute.
    monkeypatch.setattr(wh_mod, "ModelEndpoint", _ModelEndpoint)
    return wh_mod._select_api_chat_fallback_endpoint(_DB(rows), owner)


def test_sync_chat_fallback_never_picks_another_owners_endpoint(monkeypatch):
    # bob's private endpoint is first in the table, but alice must never get it.
    rows = [_ep("bob-private", "bob"), _ep("alice-private", "alice")]
    ep = _select(monkeypatch, rows, "alice")
    assert ep is not None and ep.name == "alice-private"


def test_sync_chat_fallback_prefers_owned_or_shared_only(monkeypatch):
    rows = [_ep("bob-private", "bob"), _ep("shared", None)]
    ep = _select(monkeypatch, rows, "alice")
    # Only the legacy null-owner shared row is visible to alice.
    assert ep is not None and ep.name == "shared"


def test_sync_chat_fallback_returns_none_when_only_others_endpoints(monkeypatch):
    rows = [_ep("bob-private", "bob"), _ep("carol-private", "carol")]
    # No owned/shared row → fall through to the 400, never borrow bob's key.
    assert _select(monkeypatch, rows, "alice") is None


def test_sync_chat_fallback_skips_disabled_owned_endpoint(monkeypatch):
    rows = [_ep("alice-disabled", "alice", is_enabled=False), _ep("shared", None)]
    ep = _select(monkeypatch, rows, "alice")
    assert ep is not None and ep.name == "shared"


def test_sync_chat_fallback_null_owner_uses_shared_rows_only(monkeypatch):
    # When no token owner is known, only null-owner (shared) endpoints are
    # visible — private endpoints of any user must not be returned.
    rows = [_ep("bob-private", "bob"), _ep("shared", None)]
    ep = _select(monkeypatch, rows, None)
    assert ep is not None and ep.name == "shared"


def test_sync_chat_fallback_null_owner_returns_none_with_no_shared(monkeypatch):
    # No shared rows → fail closed rather than returning another user's endpoint.
    rows = [_ep("bob-private", "bob"), _ep("alice-private", "alice")]
    assert _select(monkeypatch, rows, None) is None
