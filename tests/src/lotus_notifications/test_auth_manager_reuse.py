"""Regression tests: read-only auth checks must reuse the cached AuthManager.

Background: the Lotus scanner in the scheduler calls `known_owners()` once a
minute, and it constructed a fresh `AuthManager()` every time. Each
construction re-reads auth.json + sessions.json, reruns the migrations and logs
two INFO lines, so an idle box produced "Auth config loaded / Loaded 3
session(s) from disk" every 60s forever, drowning out everything else in the
log. The per-request policy checks did the same on every tool call.

`get_auth_manager()` already caches, with an mtime+size stamp so an
out-of-band write still gets picked up. These callers just have to use it.
"""
import importlib
import json

import pytest


def _core_auth():
    """Resolve core.auth at call time.

    Several auth tests drop it from sys.modules to re-import it against a
    freshly built `core` package, so a module-level `import core.auth` here
    would bind a stale object and every monkeypatch would land on the wrong
    module.
    """
    return importlib.import_module("core.auth")


class _FakeManager:
    is_configured = True

    def __init__(self, users=("alice", "bob")):
        self._users = users

    def list_users(self):
        return [{"username": u} for u in self._users]

    def is_admin(self, user):
        return user == "alice"


def _forbid_construction(monkeypatch, module, calls):
    """Make a direct AuthManager() in `module` a hard failure."""
    def boom(*a, **kw):
        calls.append(1)
        raise AssertionError("constructed a fresh AuthManager instead of using the cache")
    monkeypatch.setattr(module, "AuthManager", boom, raising=False)


def test_known_owners_uses_the_cache_not_a_fresh_manager(monkeypatch):
    from src.lotus_notifications import known_owners

    core_auth = _core_auth()
    constructed = []
    _forbid_construction(monkeypatch, core_auth, constructed)
    monkeypatch.setattr(core_auth, "get_auth_manager", lambda *a, **kw: _FakeManager())

    assert list(known_owners()) == ["alice", "bob"]
    assert not constructed


def test_cached_manager_is_reused_until_the_files_change(tmp_path):
    core_auth = _core_auth()
    get_auth_manager = core_auth.get_auth_manager
    reset_shared_auth_managers = core_auth.reset_shared_auth_managers
    auth_path = str(tmp_path / "auth.json")
    (tmp_path / "auth.json").write_text(
        json.dumps({"users": {"alice": {"password_hash": "x", "is_admin": True}}}),
        encoding="utf-8",
    )
    reset_shared_auth_managers()
    try:
        first = get_auth_manager(auth_path)
        assert get_auth_manager(auth_path) is first

        # An out-of-band write (another process, a manual edit) must still be
        # picked up rather than served stale from the cache.
        (tmp_path / "auth.json").write_text(
            json.dumps({"users": {
                "alice": {"password_hash": "x", "is_admin": True},
                "bob": {"password_hash": "y"},
            }}),
            encoding="utf-8",
        )
        second = get_auth_manager(auth_path)
        assert second is not first
        assert set(second.users) == {"alice", "bob"}
    finally:
        reset_shared_auth_managers()


def test_direct_construction_still_works_for_isolated_callers(tmp_path):
    # The accessor is opt-in; tests and one-off callers keep the old behavior.
    auth_path = str(tmp_path / "auth.json")
    mgr = _core_auth().AuthManager(auth_path)
    assert mgr.auth_path == auth_path
