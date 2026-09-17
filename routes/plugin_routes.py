"""Admin catalog and explicit per-session opt-in for declarative plugins."""
import json
import os
from typing import Callable
from fastapi import APIRouter, HTTPException, Request
from core.database import McpServer, ModelEndpoint, SessionLocal, get_session_settings, update_session_settings
from core.middleware import require_admin
from routes.session_routes import _verify_session_owner
from src.auth_helpers import effective_user
from src.plugin_catalog import PluginCatalog, PluginManifestError, compile_capabilities

_SETTING_KEYS = {"skills": "skill_names", "mcp_servers": "allowed_mcp_servers",
                 "tools": "enabled_tools", "models": "allowed_models"}


def _available_capabilities(request: Request, session_manager=None) -> dict[str, set[str]]:
    owner = effective_user(request)
    from services.memory.skills import SkillsManager
    from src.constants import DATA_DIR
    from src.tool_policy import known_tool_names
    skills = {str(x.get("name")) for x in SkillsManager(DATA_DIR).index_for(owner=owner) if x.get("name")}
    with SessionLocal() as db:
        mcp = {str(row.id) for row in db.query(McpServer.id).filter(McpServer.is_enabled.is_(True)).all()}
        endpoints = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled.is_(True)).all()
    try:
        from src.tool_utils import get_mcp_manager
        manager = get_mcp_manager()
        mcp.update(str(item.get("server_id")) for item in manager.get_all_tools()
                   if item.get("server_id"))
    except Exception:
        pass
    models = {str(s.model) for s in getattr(session_manager, "sessions", {}).values()
              if (not owner or getattr(s, "owner", None) == owner) and getattr(s, "model", None)}
    for endpoint in endpoints:
        hidden = set(_json_names(getattr(endpoint, "hidden_models", None)))
        models.update(name for name in [*_json_names(getattr(endpoint, "cached_models", None)),
                                        *_json_names(getattr(endpoint, "pinned_models", None))]
                      if name not in hidden)
    try:
        from src.agent_profiles import load_profiles
        for p in load_profiles():
            models.update(str(v) for v in [p.get("model"), *(p.get("model_fallbacks") or []),
                                           *(p.get("allowed_models") or [])] if v)
    except Exception:
        pass
    return {"skills": skills, "mcp_servers": mcp, "tools": set(known_tool_names()), "models": models}


def _json_names(raw) -> list[str]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []


def _is_admin(request: Request) -> bool:
    if os.getenv("AUTH_ENABLED", "true").lower() == "false":
        return True
    manager = getattr(request.app.state, "auth_manager", None)
    user = effective_user(request)
    return bool(manager and user and manager.is_admin(user))


def _policy_warnings(settings: dict, manifests: list[dict]) -> list[str]:
    caps = compile_capabilities(manifests)
    warnings = []
    if caps["skills"] and settings.get("skill_access") != "selected":
        warnings.append("Skill references are inert unless Skills access is Selected.")
    if caps["tools"] and settings.get("tool_access") != "selected":
        warnings.append("Tool references are inert unless Tool access is Selected.")
    if caps["models"] and settings.get("model_access") != "selected":
        warnings.append("Model references are inert unless Model access is Selected.")
    mcp = settings.get("allowed_mcp_servers")
    if caps["mcp_servers"] and (not mcp or "*" in mcp):
        warnings.append("MCP references are inert unless connections use an explicit selected allowlist.")
    return warnings


def _mcp_reference_status(manifests: list[dict]) -> dict[str, str]:
    """Non-secret status labels for referenced servers; never config/env data."""
    referenced = set(compile_capabilities(manifests)["mcp_servers"])
    if not referenced:
        return {}
    with SessionLocal() as db:
        configured = {str(row.id): bool(row.is_enabled) for row in db.query(McpServer.id, McpServer.is_enabled).all()}
    live = set()
    statuses = {}
    try:
        from src.tool_utils import get_mcp_manager
        manager = get_mcp_manager()
        live = {str(item.get("server_id")) for item in manager.get_all_tools() if item.get("server_id")}
        for server_id in referenced:
            raw = manager.get_server_status(server_id) if server_id in configured else {}
            if isinstance(raw, dict) and raw.get("status"):
                statuses[server_id] = str(raw["status"])
    except Exception:
        pass
    for server_id in referenced:
        if server_id in live:
            statuses[server_id] = "connected"
        elif server_id in configured and not configured[server_id]:
            statuses[server_id] = "disabled"
        elif server_id in configured:
            statuses.setdefault(server_id, "configured-disconnected")
        else:
            statuses.setdefault(server_id, "unavailable")
    return statuses


def _validate_references(caps, available):
    missing = {k: sorted(set(v) - available.get(k, set())) for k, v in caps.items()}
    missing = {k: v for k, v in missing.items() if v}
    if missing:
        detail = "; ".join(f"{k}: {', '.join(v)}" for k, v in missing.items())
        raise HTTPException(400, f"Plugin references unavailable capabilities ({detail})")


def _apply_projection(current: dict, ids: list[str], caps: dict[str, list[str]]) -> dict:
    """Project refs only into explicitly selected policies.

    ``plugin_owned_capabilities`` records only names introduced by this layer.
    Current settings minus that set are the human's source of truth, so both
    grants and revocations made while a plugin is enabled survive reapply and
    removal. Older baseline metadata is read once for safe migration.
    """
    previous_owned = current.get("plugin_owned_capabilities")
    if not isinstance(previous_owned, dict):
        previous = current.get("plugin_applied_capabilities") or {}
        baseline = current.get("plugin_base_loadout") or {}
        previous_owned = {}
        for kind in _SETTING_KEYS:
            base = baseline.get(kind) if isinstance(baseline.get(kind), dict) else {}
            previous_owned[kind] = sorted(set(previous.get(kind) or []) - set(base.get("values") or []))

    selected = {
        "skills": current.get("skill_access") == "selected",
        "tools": current.get("tool_access") == "selected",
        "models": current.get("model_access") == "selected",
        # Missing / '*' is unrestricted, [] is explicit none. Neither should
        # be silently changed into a selected allowlist by a plugin.
        "mcp_servers": bool(current.get("allowed_mcp_servers")) and
                       "*" not in (current.get("allowed_mcp_servers") or []),
    }
    patch = {"enabled_plugins": ids, "plugin_applied_capabilities": caps,
             "plugin_base_loadout": None}
    next_owned = {}
    for kind, setting in _SETTING_KEYS.items():
        was_present = setting in current
        manual = set(current.get(setting) or []) - set(previous_owned.get(kind) or [])
        additions = set(caps[kind]) - manual if selected[kind] else set()
        next_owned[kind] = sorted(additions)
        if selected[kind]:
            patch[setting] = sorted(manual | additions)
        elif was_present:
            patch[setting] = sorted(manual)
        else:
            patch[setting] = None
    patch["plugin_owned_capabilities"] = next_owned if ids else None
    if not ids:
        patch["plugin_applied_capabilities"] = None
    return patch


def setup_plugin_routes(catalog: PluginCatalog | None = None, session_manager=None,
                        capability_resolver: Callable | None = None):
    catalog = catalog or PluginCatalog()
    router = APIRouter(prefix="/api/plugins", tags=["plugins"])

    @router.get("")
    async def list_plugins(request: Request):
        require_admin(request)
        try:
            return {"plugins": catalog.list()}
        except PluginManifestError as exc:
            raise HTTPException(400, str(exc))

    @router.put("/{plugin_id}")
    async def put_plugin(request: Request, plugin_id: str):
        require_admin(request)
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.get("id") != plugin_id:
                raise PluginManifestError("path id must match manifest id")
            return catalog.save(body)
        except PluginManifestError as exc:
            raise HTTPException(400, str(exc))
        except Exception:
            raise HTTPException(400, "manifest must be valid JSON")

    @router.delete("/{plugin_id}")
    async def delete_plugin(request: Request, plugin_id: str):
        require_admin(request)
        try:
            if not catalog.delete(plugin_id):
                raise HTTPException(404, "Plugin not found")
            return {"deleted": plugin_id}
        except PluginManifestError as exc:
            raise HTTPException(400, str(exc))

    @router.get("/sessions/{session_id}/catalog")
    async def session_catalog(request: Request, session_id: str):
        _verify_session_owner(request, session_id, session_manager)
        try:
            plugins = catalog.list()
        except PluginManifestError as exc:
            raise HTTPException(400, str(exc))
        settings = get_session_settings(session_id, strict=True)
        enabled = settings.get("enabled_plugins") or []
        selected = [p for p in plugins if p["id"] in enabled] if isinstance(enabled, list) else []
        return {"plugins": plugins, "enabled_plugins": enabled if isinstance(enabled, list) else [],
                "policy_warnings": _policy_warnings(settings, selected), "can_import": _is_admin(request),
                "mcp_status": _mcp_reference_status(plugins)}

    @router.put("/sessions/{session_id}/enabled")
    async def set_enabled(request: Request, session_id: str):
        _verify_session_owner(request, session_id, session_manager)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "request body must be valid JSON")
        ids = body.get("plugin_ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not all(isinstance(v, str) for v in ids):
            raise HTTPException(400, "plugin_ids must be a list of plugin ids")
        if len(ids) > 40:
            raise HTTPException(400, "at most 40 plugins may be enabled per agent")
        ids = sorted(set(ids)); manifests = []
        for plugin_id in ids:
            try:
                manifest = catalog.get(plugin_id)
            except PluginManifestError as exc:
                raise HTTPException(400, str(exc))
            if manifest is None:
                raise HTTPException(400, f"Unknown plugin {plugin_id!r}")
            manifests.append(manifest)
        caps = compile_capabilities(manifests)
        _validate_references(caps, (capability_resolver or _available_capabilities)(request, session_manager))
        current = get_session_settings(session_id, strict=True)
        saved = update_session_settings(session_id, _apply_projection(current, ids, caps))
        if saved is None:
            raise HTTPException(500, "Failed to save plugin selection")
        return {"plugin_ids": ids, "capabilities": caps, "settings": saved,
                "policy_warnings": _policy_warnings(saved, manifests)}

    return router
