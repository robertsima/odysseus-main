"""Promoting a user to admin through the real app."""
import pytest

pytestmark = pytest.mark.security


def test_a_regular_user_cannot_promote_anyone(api):
    response = api.as_user("alice").put("/api/auth/users/bob/admin", json={"is_admin": True})

    assert response.status_code == 403
    assert not api.auth.is_admin("bob")


def test_a_regular_user_cannot_promote_themselves(api):
    response = api.as_user("alice").put("/api/auth/users/alice/admin", json={"is_admin": True})

    assert response.status_code == 403
    assert not api.auth.is_admin("alice")


def test_an_admin_can_promote_a_user(api):
    response = api.as_admin().put("/api/auth/users/bob/admin", json={"is_admin": True})

    assert response.json() == {"ok": True, "is_admin": True, "self": False}
    assert api.auth.is_admin("bob")
