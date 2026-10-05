"""Who may call the Codex and Claude integration routes."""
import io
import zipfile

import pytest

pytestmark = pytest.mark.security

# Every cookbook route: host topology, task logs, tmux sessions and model
# serving. A browser session needs admin; API tokens go by scope instead.
COOKBOOK_ROUTES = [
    ("GET", "/api/codex/cookbook/tasks"),
    ("GET", "/api/codex/cookbook/servers"),
    ("GET", "/api/codex/cookbook/output/s1"),
    ("POST", "/api/codex/cookbook/serve"),
    ("POST", "/api/codex/cookbook/stop/s1"),
    ("GET", "/api/codex/cookbook/cached"),
    ("GET", "/api/codex/cookbook/presets"),
    ("POST", "/api/codex/cookbook/preset/p1"),
    ("POST", "/api/codex/cookbook/adopt"),
]


@pytest.mark.parametrize("method,path", COOKBOOK_ROUTES)
def test_a_user_who_is_not_an_admin_cannot_reach_a_cookbook_route(api, method, path):
    kwargs = {"json": {}} if method == "POST" else {}

    response = api.as_user("alice").request(method, path, **kwargs)

    assert response.status_code == 403


def test_an_admin_reaches_the_cookbook(api):
    response = api.as_admin().get("/api/codex/cookbook/tasks")

    assert response.status_code == 200


def _api_token(api, scopes="chat"):
    response = api.as_admin().post("/api/tokens", data={"name": "integration", "scopes": scopes})
    assert response.status_code == 200, response.text
    return response.json()["token"]


@pytest.mark.parametrize("path", ["/api/codex/plugin.zip", "/api/claude/plugin.zip"])
def test_an_api_token_can_download_the_plugin_bundle(api, path):
    token = _api_token(api)

    response = api.anonymous().get(path, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200, response.text
    assert zipfile.ZipFile(io.BytesIO(response.content)).namelist()


@pytest.mark.parametrize("path", ["/api/codex/plugin.zip", "/api/claude/plugin.zip"])
def test_the_plugin_bundle_needs_a_login_or_a_token(api, path):
    assert api.anonymous().get(path).status_code == 401
