"""Regression tests for password-change session revocation."""

import pytest

pytestmark = pytest.mark.security


@pytest.fixture(autouse=True)
def _string_password_hashes(monkeypatch):
    """Skip bcrypt: hash to a readable string on the real core.auth."""
    import core.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_hash_password", lambda password: f"hash:{password}")
    monkeypatch.setattr(auth_mod, "_verify_password", lambda password, hashed: hashed == f"hash:{password}")


def _make_manager(tmp_path):
    import core.auth as auth_mod

    auth_path = tmp_path / "auth.json"
    mgr = auth_mod.AuthManager(str(auth_path))
    assert mgr.create_user("alice", "old-password", is_admin=False)
    assert mgr.create_user("bob", "bob-password", is_admin=False)
    return mgr


def test_revoke_user_sessions_preserves_current_and_persists(tmp_path):
    mgr = _make_manager(tmp_path)
    current = mgr.create_session("alice", "old-password")
    other = mgr.create_session("alice", "old-password")
    bob = mgr.create_session("bob", "bob-password")

    revoked = mgr.revoke_user_sessions("alice", except_token=current)

    assert revoked == 1
    assert mgr.validate_token(current) is True
    assert mgr.validate_token(other) is False
    assert mgr.validate_token(bob) is True


def test_wrong_current_password_does_not_revoke_sessions(tmp_path):
    mgr = _make_manager(tmp_path)
    current = mgr.create_session("alice", "old-password")
    other = mgr.create_session("alice", "old-password")

    assert mgr.change_password("alice", "wrong-password", "new-password") is False

    assert mgr.validate_token(current) is True
    assert mgr.validate_token(other) is True


def test_password_change_allows_new_password_and_blocks_old_password(tmp_path):
    mgr = _make_manager(tmp_path)

    assert mgr.change_password("alice", "old-password", "new-password") is True

    assert mgr.create_session("alice", "old-password") is None
    assert mgr.create_session("alice", "new-password") is not None


def test_create_session_trusted_rejects_username_renamed_after_verification(tmp_path):
    mgr = _make_manager(tmp_path)
    assert mgr.create_user("admin", "admin-password", is_admin=True)

    assert mgr.verify_password("alice", "old-password") is True
    assert mgr.rename_user("alice", "alice2", "admin") is True

    assert mgr.create_session_trusted("alice") is None
