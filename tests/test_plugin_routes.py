from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routes import plugin_routes as pr
from src.plugin_catalog import PluginCatalog


def _manifest():
    return {"schema_version": 1, "id": "research", "name": "Research", "version": "1",
            "capabilities": {"skills": ["review"], "mcp_servers": ["browser"],
                             "tools": [], "models": []}}


def _request(body=None):
    async def json_body():
        return body
    return SimpleNamespace(json=json_body)


def _endpoints(catalog):
    router = pr.setup_plugin_routes(catalog, session_manager=object(), capability_resolver=lambda *_: {
        "skills": {"review"}, "mcp_servers": {"browser"}, "tools": set(), "models": set()})
    return {(method, route.path): route.endpoint for route in router.routes for method in route.methods}


@pytest.mark.asyncio
async def test_catalog_routes_are_admin_gated(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(pr, "require_admin", lambda request: seen.append(request))
    eps = _endpoints(PluginCatalog(tmp_path))
    req = _request(_manifest())
    saved = await eps[("PUT", "/api/plugins/{plugin_id}")](req, "research")
    assert saved["id"] == "research"
    assert seen == [req]


@pytest.mark.asyncio
async def test_malformed_manifest_returns_400(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "require_admin", lambda request: None)
    eps = _endpoints(PluginCatalog(tmp_path))
    with pytest.raises(HTTPException) as exc:
        await eps[("PUT", "/api/plugins/{plugin_id}")](_request({"id": "research"}), "research")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_session_enable_checks_owner_and_requires_explicit_ids(tmp_path, monkeypatch):
    catalog = PluginCatalog(tmp_path)
    catalog.save(_manifest())
    owners = []
    writes = []
    monkeypatch.setattr(pr, "_verify_session_owner", lambda request, sid, mgr: owners.append(sid))
    monkeypatch.setattr(pr, "get_session_settings", lambda sid, strict=False: {
        "skill_access": "selected", "skill_names": ["manual"],
        "allowed_mcp_servers": ["manual-mcp"], "enabled_tools": [], "allowed_models": []})
    monkeypatch.setattr(pr, "update_session_settings", lambda sid, patch: writes.append(patch) or patch)
    eps = _endpoints(catalog)
    out = await eps[("PUT", "/api/plugins/sessions/{session_id}/enabled")](
        _request({"plugin_ids": ["research"]}), "chat-1")
    assert owners == ["chat-1"]
    assert out["plugin_ids"] == ["research"]
    assert writes[0]["skill_names"] == ["manual", "review"]
    assert writes[0]["allowed_mcp_servers"] == ["browser", "manual-mcp"]


@pytest.mark.asyncio
async def test_unknown_plugin_does_not_change_session(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "_verify_session_owner", lambda *args: None)
    monkeypatch.setattr(pr, "update_session_settings", lambda *args: pytest.fail("must not write"))
    eps = _endpoints(PluginCatalog(tmp_path))
    with pytest.raises(HTTPException) as exc:
        await eps[("PUT", "/api/plugins/sessions/{session_id}/enabled")](
            _request({"plugin_ids": ["missing"]}), "chat-1")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_disabling_restores_manual_baseline(tmp_path, monkeypatch):
    catalog = PluginCatalog(tmp_path)
    catalog.save(_manifest())
    state = {"skill_access": "selected", "skill_names": ["manual"], "allowed_mcp_servers": ["manual-mcp"],
             "enabled_tools": [], "allowed_models": []}
    monkeypatch.setattr(pr, "_verify_session_owner", lambda *args: None)
    monkeypatch.setattr(pr, "get_session_settings", lambda *args, **kwargs: dict(state))
    def save(_sid, patch):
        for key, value in patch.items():
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value
        return dict(state)
    monkeypatch.setattr(pr, "update_session_settings", save)
    endpoint = _endpoints(catalog)[("PUT", "/api/plugins/sessions/{session_id}/enabled")]
    await endpoint(_request({"plugin_ids": ["research"]}), "chat-1")
    assert state["skill_names"] == ["manual", "review"]
    await endpoint(_request({"plugin_ids": []}), "chat-1")
    assert state["skill_names"] == ["manual"]
    assert state["allowed_mcp_servers"] == ["manual-mcp"]
    assert "plugin_base_loadout" not in state


@pytest.mark.asyncio
async def test_manual_grants_added_while_enabled_survive_removal(tmp_path, monkeypatch):
    catalog = PluginCatalog(tmp_path); catalog.save(_manifest())
    state = {"skill_access": "selected", "skill_names": ["manual"], "allowed_mcp_servers": [], "enabled_tools": [], "allowed_models": []}
    monkeypatch.setattr(pr, "_verify_session_owner", lambda *args: None)
    monkeypatch.setattr(pr, "get_session_settings", lambda *args, **kwargs: dict(state))
    def save(_sid, patch):
        for key, value in patch.items():
            state.pop(key, None) if value is None else state.__setitem__(key, value)
        return dict(state)
    monkeypatch.setattr(pr, "update_session_settings", save)
    endpoint = _endpoints(catalog)[("PUT", "/api/plugins/sessions/{session_id}/enabled")]
    await endpoint(_request({"plugin_ids": ["research"]}), "chat-1")
    state["skill_names"].append("later-manual")
    await endpoint(_request({"plugin_ids": []}), "chat-1")
    assert state["skill_names"] == ["later-manual", "manual"]


def test_absent_allowlists_stay_absent_and_explicit_empty_stays_empty():
    absent = pr._apply_projection({}, ["p"], {"skills": [], "mcp_servers": ["browser"], "tools": [], "models": ["m"]})
    assert absent["allowed_mcp_servers"] is None and absent["allowed_models"] is None
    explicit = pr._apply_projection({"allowed_mcp_servers": [], "allowed_models": [], "skill_names": [], "enabled_tools": []},
                                    [], {"skills": [], "mcp_servers": [], "tools": [], "models": []})
    assert explicit["allowed_mcp_servers"] == [] and explicit["allowed_models"] == []


def test_explicit_policy_added_after_inherited_baseline_survives_removal():
    current = {
        "plugin_base_loadout": {
            "skills": {"present": False, "values": []}, "mcp_servers": {"present": False, "values": []},
            "tools": {"present": False, "values": []}, "models": {"present": False, "values": []}},
        "plugin_applied_capabilities": {"skills": [], "mcp_servers": ["plugin-mcp"], "tools": [], "models": []},
        "allowed_mcp_servers": ["plugin-mcp", "manual-mcp"],
    }
    patch = pr._apply_projection(current, [], {"skills": [], "mcp_servers": [], "tools": [], "models": []})
    assert patch["allowed_mcp_servers"] == ["manual-mcp"]


def test_projection_never_changes_disabled_tool_precedence():
    current = {"enabled_tools": [], "disabled_tools": ["web_search"], "skill_names": [],
               "allowed_mcp_servers": [], "allowed_models": []}
    patch = pr._apply_projection(current, ["p"], {
        "skills": [], "mcp_servers": [], "tools": ["web_search"], "models": []})
    assert "disabled_tools" not in patch


def test_manual_revocation_of_original_grant_survives_reapply_and_remove():
    caps = {"skills": [], "mcp_servers": [], "tools": ["fetch_url"], "models": []}
    initial = {"tool_access": "selected", "enabled_tools": ["web_search"],
               "skill_names": [], "allowed_mcp_servers": [], "allowed_models": []}
    first = pr._apply_projection(initial, ["p"], caps)
    current = dict(initial); current.update({k: v for k, v in first.items() if v is not None})
    current["enabled_tools"] = ["fetch_url"]  # human revoked original web_search
    reapplied = pr._apply_projection(current, ["p"], caps)
    assert reapplied["enabled_tools"] == ["fetch_url"]
    current.update({k: v for k, v in reapplied.items() if v is not None})
    removed = pr._apply_projection(current, [], {k: [] for k in caps})
    assert removed["enabled_tools"] == [] and "web_search" not in removed["enabled_tools"]


def test_explicit_none_policies_are_never_broadened():
    current = {"skill_access": "none", "tool_access": "none", "model_access": "current",
               "skill_names": [], "enabled_tools": [], "allowed_models": [], "allowed_mcp_servers": []}
    patch = pr._apply_projection(current, ["p"], {
        "skills": ["review"], "tools": ["web_search"], "models": ["m"], "mcp_servers": ["browser"]})
    assert patch["skill_names"] == patch["enabled_tools"] == patch["allowed_models"] == patch["allowed_mcp_servers"] == []
    assert not ({"skill_access", "tool_access", "model_access"} & set(patch))


@pytest.mark.asyncio
async def test_malformed_apply_json_is_400(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "_verify_session_owner", lambda *args: None)
    router = pr.setup_plugin_routes(PluginCatalog(tmp_path), capability_resolver=lambda *_: {})
    eps = {(m, r.path): r.endpoint for r in router.routes for m in r.methods}
    async def bad_json(): raise ValueError("bad")
    with pytest.raises(HTTPException) as exc:
        await eps[("PUT", "/api/plugins/sessions/{session_id}/enabled")](SimpleNamespace(json=bad_json), "chat-1")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_apply_rejects_unavailable_references(tmp_path, monkeypatch):
    catalog = PluginCatalog(tmp_path); catalog.save(_manifest())
    monkeypatch.setattr(pr, "_verify_session_owner", lambda *args: None)
    router = pr.setup_plugin_routes(catalog, capability_resolver=lambda *_: {
        "skills": set(), "mcp_servers": set(), "tools": set(), "models": set()})
    eps = {(m, r.path): r.endpoint for r in router.routes for m in r.methods}
    with pytest.raises(HTTPException) as exc:
        await eps[("PUT", "/api/plugins/sessions/{session_id}/enabled")](
            _request({"plugin_ids": ["research"]}), "chat-1")
    assert "skills: review" in exc.value.detail and "mcp_servers: browser" in exc.value.detail


@pytest.mark.asyncio
async def test_enable_payload_caps_plugin_count(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "_verify_session_owner", lambda *args: None)
    endpoint = _endpoints(PluginCatalog(tmp_path))[("PUT", "/api/plugins/sessions/{session_id}/enabled")]
    with pytest.raises(HTTPException) as exc:
        await endpoint(_request({"plugin_ids": [f"p{i}" for i in range(41)]}), "chat-1")
    assert exc.value.status_code == 400 and "at most 40" in exc.value.detail


def test_actual_resolver_includes_cached_and_pinned_models_without_secrets(monkeypatch):
    import services.memory.skills as skill_module
    import src.tool_utils as tool_utils
    monkeypatch.setattr(skill_module.SkillsManager, "index_for", lambda self, owner=None: [{"name": "mine"}])
    endpoint = SimpleNamespace(cached_models='["cached", "hidden"]', pinned_models='["pinned"]',
                               hidden_models='["hidden"]')
    class Query:
        def __init__(self, target): self.target = target
        def filter(self, *args): return self
        def all(self):
            return [SimpleNamespace(id="enabled-mcp")] if self.target is pr.McpServer.id else [endpoint]
    class DB:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def query(self, target, *args): return Query(target)
    monkeypatch.setattr(pr, "SessionLocal", lambda: DB())
    monkeypatch.setattr(pr, "effective_user", lambda request: "alice")
    monkeypatch.setattr(tool_utils, "get_mcp_manager", lambda: None)
    request = SimpleNamespace()
    manager = SimpleNamespace(sessions={})
    available = pr._available_capabilities(request, manager)
    assert {"cached", "pinned"} <= available["models"]
    assert "hidden" not in available["models"]
    assert available["skills"] == {"mine"} and available["mcp_servers"] == {"enabled-mcp"}
    assert all("key" not in value.lower() for values in available.values() for value in values)
