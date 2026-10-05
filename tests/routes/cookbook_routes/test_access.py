"""The cookbook runs processes and holds SSH keys, so it is closed to anonymous callers and non-admins."""
import pytest

pytestmark = pytest.mark.security

# Bodies that pass request validation, which runs before the admin check.
BODIES = {
    "/api/cookbook/test-ssh": {"host": "gpu-box"},
    "/api/model/download": {"repo_id": "org/model"},
    "/api/model/serve": {"repo_id": "org/model", "cmd": "true"},
    "/api/cookbook/setup": {"host": "gpu-box"},
    "/api/cookbook/kill-pid": {"pid": 1},
}

ADMIN_ONLY = [
    ("GET", "/api/cookbook/ssh-key"),
    ("POST", "/api/cookbook/ssh-key"),
    ("POST", "/api/cookbook/test-ssh"),
    ("POST", "/api/model/download"),
    ("GET", "/api/model/cached"),
    ("POST", "/api/model/serve"),
    ("POST", "/api/cookbook/setup"),
    ("GET", "/api/cookbook/gpus"),
    ("POST", "/api/cookbook/kill-pid"),
    ("GET", "/api/cookbook/state"),
    ("POST", "/api/cookbook/state"),
    ("GET", "/api/cookbook/tasks/status"),
]

LOGIN_ONLY = [
    ("GET", "/api/cookbook/hf-latest"),
    ("GET", "/api/cookbook/hf-gguf-files?repo_id=x/y"),
    ("GET", "/api/cookbook/ollama/library"),
]


@pytest.mark.parametrize("method, path", ADMIN_ONLY + LOGIN_ONLY)
def test_a_request_without_a_session_is_refused(api, method, path):
    response = api.anonymous().request(method, path, json=BODIES.get(path, {}))

    assert response.status_code == 401


@pytest.mark.parametrize("method, path", ADMIN_ONLY)
def test_a_logged_in_user_who_is_not_an_admin_is_refused(api, method, path):
    response = api.as_user("alice").request(method, path, json=BODIES.get(path, {}))

    assert response.status_code == 403


def test_an_admin_can_read_the_cookbook_state(api):
    response = api.as_admin().get("/api/cookbook/state")

    assert response.status_code == 200
    assert isinstance(response.json(), dict)
