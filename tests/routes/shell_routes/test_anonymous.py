"""The shell and package endpoints turn away a request with no session cookie."""
import pytest

pytestmark = pytest.mark.security

ENDPOINTS = [
    ("POST", "/api/shell/exec"),
    ("POST", "/api/shell/stream"),
    ("GET", "/api/cookbook/packages"),
    ("POST", "/api/cookbook/packages/install"),
    ("POST", "/api/cookbook/install-system-deps"),
]


@pytest.mark.parametrize("method, path", ENDPOINTS)
def test_a_request_without_a_session_is_refused(api, method, path):
    response = api.anonymous().request(method, path, json={"command": " "})

    assert response.status_code == 401
