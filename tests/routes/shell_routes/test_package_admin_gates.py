"""Installing packages is code execution, so only admins may start it or list the host's packages."""
import pytest

pytestmark = pytest.mark.security

ADMIN_ONLY = [
    ("GET", "/api/cookbook/packages"),
    ("POST", "/api/cookbook/packages/install"),
    ("POST", "/api/cookbook/install-system-deps"),
]


@pytest.mark.parametrize("method, path", ADMIN_ONLY)
def test_a_logged_in_user_who_is_not_an_admin_is_refused(api, method, path):
    response = api.as_user("alice").request(method, path, json={})

    assert response.status_code == 403


def test_an_admin_asking_to_install_nothing_is_told_what_is_missing(api):
    response = api.as_admin().post("/api/cookbook/packages/install", json={})

    assert response.status_code == 200
    assert response.json() == {"ok": False, "error": "No package specified"}
