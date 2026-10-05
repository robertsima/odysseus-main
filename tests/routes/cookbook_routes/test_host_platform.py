"""The platform of the machine Odysseus runs on, as the Cookbook state reports it.

The browser decides how to build serve and install commands (a local Windows
host runs Git Bash, not tmux) from `env.hostPlatform` in GET
/api/cookbook/state. It has to describe the backend host, not what a client
last saved: a browser on Linux that synced its own state to a Windows host
must not make the Cookbook build Linux commands for it.
"""
from routes import cookbook_routes


def _state(api):
    response = api.as_admin().get("/api/cookbook/state")
    assert response.status_code == 200, response.text
    return response.json()


def test_a_windows_backend_reports_windows_as_the_host_platform(api, monkeypatch):
    monkeypatch.setattr(cookbook_routes, "IS_WINDOWS", True)
    assert _state(api)["env"]["hostPlatform"] == "windows"


def test_a_saved_host_platform_does_not_override_the_backend(api, monkeypatch):
    monkeypatch.setattr(cookbook_routes, "IS_WINDOWS", False)
    saved = api.as_admin().post("/api/cookbook/state", json={"env": {"hostPlatform": "windows"}, "tasks": []})
    assert saved.status_code == 200, saved.text

    assert _state(api)["env"]["hostPlatform"] == ""
