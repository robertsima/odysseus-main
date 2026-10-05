"""The platform of the machine Odysseus runs on, as the Cookbook state reports it.

The browser decides how to build serve and install commands (a local Windows
host runs Git Bash, not tmux) from `env.hostPlatform` in GET
/api/cookbook/state. It has to describe the backend host, not what a client
last saved: a browser on Linux that synced its own state to a Windows host
must not make the Cookbook build Linux commands for it.
"""


def _host_is_windows(monkeypatch, value):
    """Set IS_WINDOWS on every loaded copy of routes.cookbook_routes.

    Patching the module by import is not enough: when an earlier test in the
    process re-imported it, the app still serves the functions of the copy it
    loaded first, which read their own IS_WINDOWS.
    """
    import gc
    import types

    copies = [obj for obj in gc.get_objects()
              if isinstance(obj, types.ModuleType) and getattr(obj, "__name__", "") == "routes.cookbook_routes"]
    assert copies, "routes.cookbook_routes is not loaded"
    for module in copies:
        monkeypatch.setattr(module, "IS_WINDOWS", value)


def _state(api):
    response = api.as_admin().get("/api/cookbook/state")
    assert response.status_code == 200, response.text
    return response.json()


def test_a_windows_backend_reports_windows_as_the_host_platform(api, monkeypatch):
    _host_is_windows(monkeypatch, True)
    assert _state(api)["env"]["hostPlatform"] == "windows"


def test_a_saved_host_platform_does_not_override_the_backend(api, monkeypatch):
    _host_is_windows(monkeypatch, False)
    saved = api.as_admin().post("/api/cookbook/state", json={"env": {"hostPlatform": "windows"}, "tasks": []})
    assert saved.status_code == 200, saved.text

    assert _state(api)["env"]["hostPlatform"] == ""
