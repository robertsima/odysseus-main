"""Admin catalog and explicit per-session opt-in for declarative plugins.

Schema v2 packages (user-added integrations) are installed here, by an admin,
explicitly: ``POST /api/plugins/{id}/install`` creates the MCP server row
through the same endpoint ``POST /api/mcp/servers`` serves, imports the
package's skills as the admin's own draft skills and its loadout templates
through profile import, and records exactly what it made so
``DELETE /api/plugins/{id}/install`` removes that and nothing else.
"""
import json
import logging
import os
import re
from typing import Any, Callable
from fastapi import APIRouter, HTTPException, Request
from core.database import McpServer, ModelEndpoint, SessionLocal, get_session_settings, update_session_settings
from core.middleware import require_admin
from routes.session_routes import _verify_session_owner
from src.auth_helpers import effective_user
from src.plugin_catalog import PluginCatalog, PluginManifestError, compile_capabilities

logger = logging.getLogger(__name__)

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


def _mcp_endpoint(method: str, path: str) -> Callable:
    """The handler behind an /api/mcp route, so install creates a server by the
    very code path the Settings form uses (validation, OAuth env, DB row,
    connect) instead of a second implementation that could drift from it."""
    from routes.mcp import mcp_routes
    for route in mcp_routes.router.routes:
        if getattr(route, "path", "") == path and method in (getattr(route, "methods", None) or ()):
            return route.endpoint
    raise HTTPException(503, "MCP routes are not available")


async def _default_mcp_add(request: Request, spec: dict, env: dict) -> dict:
    endpoint = _mcp_endpoint("POST", "/api/mcp/servers")
    return await endpoint(
        request, name=spec["name"], transport=spec["transport"], command=spec.get("command"),
        args=json.dumps(spec.get("args") or []), env=json.dumps(env), url=spec.get("url"),
        oauth_file=None, oauth_config=None)


async def _default_mcp_remove(request: Request, server_id: str) -> None:
    endpoint = _mcp_endpoint("DELETE", "/api/mcp/servers/{server_id}")
    try:
        await endpoint(server_id, request)
    except HTTPException as exc:
        if exc.status_code != 404:  # already gone is the goal state
            raise


def _command_line(spec: dict) -> str:
    """What the admin approves: the exact command, or the URL, verbatim."""
    if spec["transport"] == "stdio":
        return " ".join([spec["command"], *spec.get("args", [])])
    return spec["url"]


_ENV_VALUE_LIMIT = 4096


def _clean_env(spec: dict, given: Any) -> dict:
    """Env values the admin typed, restricted to the names the package declared.

    Values go to the MCP server row and nowhere else: not the manifest, not the
    install record, not the response.
    """
    given = given if isinstance(given, dict) else {}
    declared = {e["name"]: e for e in spec.get("env", [])}
    extra = sorted(set(given) - set(declared))
    if extra:
        raise HTTPException(400, f"Not declared by the package: {', '.join(extra)}")
    out = {}
    for name, entry in declared.items():
        value = given.get(name)
        if value is None or value == "":
            if entry["required"]:
                raise HTTPException(400, f"{name} is required")
            continue
        if not isinstance(value, str) or len(value) > _ENV_VALUE_LIMIT or re.search(r"[\x00-\x1f]", value):
            raise HTTPException(400, f"{name} must be a single-line string")
        out[name] = value
    return out


def _effective(manifest: dict, record: dict | None) -> dict:
    """A manifest plus the server id its install created, so enabling the plugin
    for a chat grants that server (the manifest cannot know the generated id)."""
    server_id = (record or {}).get("server_id")
    if not server_id:
        return manifest
    caps = dict(manifest["capabilities"])
    caps["mcp_servers"] = sorted({*caps.get("mcp_servers", []), str(server_id)})
    return {**manifest, "capabilities": caps}


def setup_plugin_routes(catalog: PluginCatalog | None = None, session_manager=None,
                        capability_resolver: Callable | None = None,
                        mcp_add: Callable | None = None, mcp_remove: Callable | None = None,
                        skills_manager=None):
    catalog = catalog or PluginCatalog()
    router = APIRouter(prefix="/api/plugins", tags=["plugins"])
    add_server = mcp_add or _default_mcp_add
    remove_server = mcp_remove or _default_mcp_remove

    def _skills():
        if skills_manager is not None:
            return skills_manager
        from services.memory.skills import SkillsManager
        from src.constants import DATA_DIR
        return SkillsManager(DATA_DIR)

    def _installs(manifests: list[dict]) -> dict[str, dict]:
        """Non-secret summary per installed plugin, for the UI."""
        out = {}
        for manifest in manifests:
            rec = catalog.install_record(manifest["id"])
            if rec:
                out[manifest["id"]] = {k: rec.get(k) for k in
                                       ("server_id", "skills", "loadouts", "published", "installed_at")}
        return out

    @router.get("")
    async def list_plugins(request: Request):
        require_admin(request)
        try:
            plugins = catalog.list()
        except PluginManifestError as exc:
            raise HTTPException(400, str(exc))
        return {"plugins": plugins, "installs": _installs(plugins)}

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

    @router.post("/{plugin_id}/install")
    async def install_plugin(request: Request, plugin_id: str):
        """Install a v2 package. Admin only. ``approved`` must echo the command
        or URL the admin was shown, so a manifest re-imported after the page
        rendered cannot run something the admin did not read."""
        require_admin(request)
        try:
            manifest = catalog.get(plugin_id)
        except PluginManifestError as exc:
            raise HTTPException(400, str(exc))
        if manifest is None:
            raise HTTPException(404, "Plugin not found")
        block = manifest.get("integration")
        if not block:
            raise HTTPException(400, "This plugin has no integration block to install")
        if catalog.install_record(plugin_id):
            raise HTTPException(409, "Already installed; uninstall it first")
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        spec = block.get("mcp_server")
        env = _clean_env(spec, body.get("env")) if spec else {}
        if spec and body.get("approved") != _command_line(spec):
            raise HTTPException(
                409, "The command or URL changed since you reviewed it; reload and approve it again")
        owner = effective_user(request) or ""
        publish = body.get("publish_skills") is True
        record: dict[str, Any] = {
            "plugin_id": plugin_id, "version": manifest["version"], "name": block["name"],
            "instructions": block.get("instructions", ""), "server_id": None,
            "skills": [], "skill_owner": owner, "loadouts": [], "published": publish,
        }
        report: dict[str, Any] = {"plugin_id": plugin_id, "server": None, "skills": [],
                                  "loadouts": [], "unresolved_references": [], "loadout_errors": []}
        try:
            if spec:
                created = await add_server(request, spec, env)
                record["server_id"] = created.get("id")
                report["server"] = {k: created.get(k) for k in
                                    ("id", "name", "connected", "status", "tool_count", "error", "needs_auth")}
                # Persist before anything else can fail, so a crash still leaves
                # a record uninstall can follow.
                catalog.write_install_record(record)
            _install_skills(block, record, report, owner, publish)
            _install_loadouts(block, record, report)
            catalog.write_install_record(record)
        except HTTPException:
            await _undo(request, record)
            raise
        except Exception as exc:
            logger.warning("plugin install %s failed: %s", plugin_id, exc, exc_info=True)
            await _undo(request, record)
            raise HTTPException(400, f"Install failed and was rolled back: {exc}")
        return report

    def _install_skills(block, record, report, owner, publish):
        manager = _skills()
        for item in block.get("skills", []):
            made = manager.import_bundle_from_files(
                {f"{item['name']}/SKILL.md": item["content"]}, owner=owner or None,
                source_url=f"plugin:{record['plugin_id']}")
            name = made["name"]
            record["skills"].append(name)
            updates: dict[str, Any] = {}
            if record.get("server_id"):
                # Visible only while the plugin's server is connected
                # (integration_registry.available_ids includes installed plugins).
                updates["requires_integration"] = record["plugin_id"]
            if publish:
                updates["status"] = "published"
            if updates:
                manager.update_skill(name, updates, owner=owner or None)
            report["skills"].append({"name": name, "status": "published" if publish else "draft"})

    def _install_loadouts(block, record, report):
        if not block.get("loadout_templates"):
            return
        from src import agent_profile_transfer
        # getattr: resolve_template landed separately; without it a template
        # that uses {server:<name>} simply fails profile validation and is reported.
        resolve = getattr(agent_profile_transfer, "resolve_template", None)
        manager = None
        try:
            from src.tool_utils import get_mcp_manager
            manager = get_mcp_manager()
        except Exception:
            pass
        for doc in block["loadout_templates"]:
            if resolve is not None:
                doc, notes = resolve(doc, manager)
                report["unresolved_references"].extend(notes)
            result = agent_profile_transfer.import_profiles(doc, mode="merge", rename_conflicts=True)
            record["loadouts"].extend(result.get("added", []))
            report["loadouts"].extend(result.get("added", []))
            report["loadout_errors"].extend(result.get("errors", []))

    async def _undo(request, record):
        """Remove what this record says an install made; best effort, never raises."""
        removed: dict[str, Any] = {"server": None, "skills": [], "loadouts": []}
        if record.get("loadouts"):
            from src import agent_loadouts
            for name in record["loadouts"]:
                try:
                    if agent_loadouts.delete(name):
                        removed["loadouts"].append(name)
                except Exception:
                    logger.warning("could not remove loadout %s", name, exc_info=True)
        if record.get("skills"):
            manager = _skills()
            for name in record["skills"]:
                try:
                    if manager.delete_skill(name, owner=record.get("skill_owner") or None):
                        removed["skills"].append(name)
                except Exception:
                    logger.warning("could not remove skill %s", name, exc_info=True)
        if record.get("server_id"):
            try:
                await remove_server(request, record["server_id"])
                removed["server"] = record["server_id"]
            except Exception:
                logger.warning("could not remove MCP server %s", record["server_id"], exc_info=True)
        catalog.delete_install_record(record["plugin_id"])
        return removed

    @router.delete("/{plugin_id}/install")
    async def uninstall_plugin(request: Request, plugin_id: str):
        require_admin(request)
        record = catalog.install_record(plugin_id)
        if not record:
            raise HTTPException(404, "Plugin is not installed")
        return {"plugin_id": plugin_id, "removed": await _undo(request, record)}

    @router.delete("/{plugin_id}")
    async def delete_plugin(request: Request, plugin_id: str):
        require_admin(request)
        if catalog.install_record(plugin_id):
            raise HTTPException(409, "Uninstall this plugin before deleting its manifest")
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
                "mcp_status": _mcp_reference_status(plugins), "installs": _installs(plugins)}

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
            manifests.append(_effective(manifest, catalog.install_record(plugin_id)))
        caps = compile_capabilities(manifests)
        _validate_references(caps, (capability_resolver or _available_capabilities)(request, session_manager))
        current = get_session_settings(session_id, strict=True)
        saved = update_session_settings(session_id, _apply_projection(current, ids, caps))
        if saved is None:
            raise HTTPException(500, "Failed to save plugin selection")
        return {"plugin_ids": ids, "capabilities": caps, "settings": saved,
                "policy_warnings": _policy_warnings(saved, manifests)}

    return router
