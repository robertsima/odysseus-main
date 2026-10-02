"""The integration registry: one place that knows what each built-in integration is.

An integration used to be spread over four hand-kept lists (builtin_mcp's server
tables, its catalog, mcp_manager's function-calling set and is_builtin) plus three
registries that each held a slice (website/architecture-integrations-2026-10-01.md).
Each built-in now declares itself in ``integrations/<dir>/integration.json`` and
this module derives every list from those files. Code stays where it is; only the
facts moved.

Manifest schema (JSON, validated on load; a malformed shipped manifest raises
``IntegrationManifestError`` so the mistake shows up in tests, not as a silently
missing integration)::

    id                str   unique; the integration's name ("penpot", "pi-worker")
    name              str   label in Settings > Built-in
    description       str
    kind              str   "mcp-python" | "mcp-npx" | "mcp-binary" | "native"
    capability        str   native only: the src.capabilities name behind it
    servers           list  MCP kinds: one entry per MCP server, each
        id            str   the server id; tool names are mcp__<id>__<tool>, so
                            these stay as they were (penpot_studio, pi_worker, ...)
        name          str   the connection name ("Built-in: Todoist")
        title, description  optional; the Settings row when an integration has
                            several servers (GitHub read / write)
        script        str   mcp-python: path relative to the app root
        command       str | {"callable": "module:function"}   mcp-npx / mcp-binary;
                            a callable returns the executable path, "" when absent
        args          list  may contain "{tools}", replaced by the server's tools
        tools         list  the explicit tool allow-list passed to the server
        env_profile   str   "python_path" | "github" | "browser_cache" | "none";
                            the environment builders stay in builtin_mcp
        requirements  list  per-server requirements (see below)
    requirements      list  needed by every server of the integration
        name, hint    str   hint says what to DO, not what went wrong
        check         obj   see "checks"
        gates_start   bool  true: the server is not started while this is unmet
                            (Pi worker without ODYSSEUS_PI_WORKER_HOST). False:
                            it starts anyway and status reports not_configured.
    settings          list[str] | "from_capability"   settings keys it owns
    tools             list[str] | "from_server" | "from_capability"
    function_calling  bool  true: its tools are sent as native function schemas
                            and gated per turn (was _BUILTIN_FUNCTION_CALLING_SERVERS)
    prompt            str   short routing text, "from_server_instructions", or ""
    skills            list  skill directories relative to the integration's folder
    loadouts          list  loadout template files relative to the same folder
    health            obj   {"type": "mcp_connected"} | {"type": "capability",
                            "name": ...} | {"type": "none"}
    docs              str   URL or repo path
    order             int   position in Settings > Built-in (default 100)

Checks (``requirements[].check``): ``env`` {var}, ``env_flag`` {var} (1/true/yes),
``env_all`` {vars}, ``setting`` {key}, ``binary`` {names}, ``capability`` {name}
(the capability is enabled-independent satisfied), ``callable`` {ref} (truthy
return, a ``(ok, detail)`` tuple, or an exception for "unmet"), ``any_of`` /
``all_of`` {checks}.

Interface (everything a caller needs):

    all() -> tuple[Integration, ...]
    get(id) -> Integration | None
    builtin_mcp_specs() -> dict[server_id, dict]
    is_builtin(server_id) -> bool
    function_calling_server_ids() -> frozenset[str]
    catalog_rows() -> list[dict]
    status(id, manager=None) -> {enabled, configured, connected, missing}
    server_status(server_id, manager=None) -> same shape, for one server
    available_ids(manager=None) -> set[str]
    integration_for_tool(tool_name) -> str | None
    integration_for_server(server_id) -> str | None
    skill_dirs(id) -> list[Path]
    loadout_templates(id) -> list[Path]
    startable(server_id) -> bool

Plugin-installed integrations (kind "plugin", from src.plugin_catalog install
records) are separate from all(): plugin_integrations(), plugin_instructions(),
plugin_integration_for_server(). available_ids() and integration_for_tool()
include them; is_builtin() and the pinned lists never do.
"""

from __future__ import annotations

import builtins
import importlib
import json
import logging
import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.runtime_paths import get_app_root

logger = logging.getLogger(__name__)

KINDS = ("mcp-python", "mcp-npx", "mcp-binary", "native")
_MCP_KINDS = ("mcp-python", "mcp-npx", "mcp-binary")
_ENV_PROFILES = ("python_path", "github", "browser_cache", "none")
_HEALTH_TYPES = ("mcp_connected", "capability", "none")
_CHECK_TYPES = ("env", "env_flag", "env_all", "setting", "binary", "capability", "callable", "any_of", "all_of")
_TRUTHY = ("1", "true", "yes")
MANIFEST_NAME = "integration.json"


class IntegrationManifestError(ValueError):
    """A manifest is malformed. The message names the file and the field."""


@dataclass(frozen=True)
class Requirement:
    name: str
    check: dict
    hint: str = ""
    gates_start: bool = False


@dataclass(frozen=True)
class Server:
    id: str
    name: str
    title: str = ""
    description: str = ""
    script: str = ""
    command: Any = ""
    args: Tuple[str, ...] = ()
    tools: Tuple[str, ...] = ()
    env_profile: str = "none"
    requirements: Tuple[Requirement, ...] = ()


@dataclass(frozen=True)
class Integration:
    id: str
    name: str
    description: str
    kind: str
    directory: Path
    capability: str = ""
    servers: Tuple[Server, ...] = ()
    requirements: Tuple[Requirement, ...] = ()
    settings: Any = ()
    tools: Any = "from_server"
    function_calling: bool = False
    prompt: str = ""
    skills: Tuple[str, ...] = ()
    loadouts: Tuple[str, ...] = ()
    health: dict = field(default_factory=lambda: {"type": "none"})
    docs: str = ""
    order: int = 100

    @property
    def is_mcp(self) -> bool:
        return self.kind in _MCP_KINDS


# ── loading and validation ───────────────────────────────────────────────


def _fail(path: Path, message: str):
    raise IntegrationManifestError(f"{path}: {message}")


def _str(path: Path, data: dict, key: str, *, required: bool = True, default: str = "") -> str:
    value = data.get(key, default)
    if value is None or value == "":
        if required:
            _fail(path, f"{key!r} is required")
        return default
    if not isinstance(value, str):
        _fail(path, f"{key!r} must be a string")
    return value


def _str_list(path: Path, value: Any, label: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not builtins.all(isinstance(v, str) and v for v in value):
        _fail(path, f"{label} must be a list of non-empty strings")
    return tuple(value)


def _validate_check(path: Path, check: Any, label: str) -> None:
    if not isinstance(check, dict) or check.get("type") not in _CHECK_TYPES:
        _fail(path, f"{label}.type must be one of {', '.join(_CHECK_TYPES)}")
    kind = check["type"]
    if kind in ("env", "env_flag") and not isinstance(check.get("var"), str):
        _fail(path, f"{label}.var is required")
    elif kind == "env_all":
        _str_list(path, check.get("vars"), f"{label}.vars")
    elif kind == "setting" and not isinstance(check.get("key"), str):
        _fail(path, f"{label}.key is required")
    elif kind == "binary":
        _str_list(path, check.get("names"), f"{label}.names")
    elif kind == "capability" and not isinstance(check.get("name"), str):
        _fail(path, f"{label}.name is required")
    elif kind == "callable":
        ref = check.get("ref")
        if not isinstance(ref, str) or ":" not in ref:
            _fail(path, f"{label}.ref must look like 'module:function'")
    elif kind in ("any_of", "all_of"):
        children = check.get("checks")
        if not isinstance(children, list) or not children:
            _fail(path, f"{label}.checks must be a non-empty list")
        for i, child in enumerate(children):
            _validate_check(path, child, f"{label}.checks[{i}]")


def _requirements(path: Path, raw: Any, label: str) -> Tuple[Requirement, ...]:
    if raw in (None, []):
        return ()
    if not isinstance(raw, list):
        _fail(path, f"{label} must be a list")
    out = []
    for i, item in enumerate(raw):
        where = f"{label}[{i}]"
        if not isinstance(item, dict):
            _fail(path, f"{where} must be an object")
        _validate_check(path, item.get("check"), f"{where}.check")
        out.append(Requirement(
            name=_str(path, item, "name"),
            check=item["check"],
            hint=_str(path, item, "hint", required=False),
            gates_start=bool(item.get("gates_start", False)),
        ))
    return tuple(out)


def _server(path: Path, kind: str, raw: Any, index: int) -> Server:
    where = f"servers[{index}]"
    if not isinstance(raw, dict):
        _fail(path, f"{where} must be an object")
    env_profile = raw.get("env_profile", "none")
    if env_profile not in _ENV_PROFILES:
        _fail(path, f"{where}.env_profile must be one of {', '.join(_ENV_PROFILES)}")
    tools = _str_list(path, raw["tools"], f"{where}.tools") if "tools" in raw else ()
    args = _str_list(path, raw.get("args", []), f"{where}.args")
    command: Any = raw.get("command", "")
    script = raw.get("script", "")
    if kind == "mcp-python":
        if not script:
            _fail(path, f"{where}.script is required for mcp-python")
    else:
        if isinstance(command, dict):
            ref = command.get("callable")
            if not isinstance(ref, str) or ":" not in ref:
                _fail(path, f"{where}.command.callable must look like 'module:function'")
        elif not isinstance(command, str) or not command:
            _fail(path, f"{where}.command is required for {kind}")
        if any("{tools}" in a for a in args) and not tools:
            _fail(path, f"{where}.args uses {{tools}} but the server lists no tools")
    return Server(
        id=_str(path, raw, "id"),
        name=_str(path, raw, "name"),
        title=_str(path, raw, "title", required=False),
        description=_str(path, raw, "description", required=False),
        script=script,
        command=command,
        args=args,
        tools=tools,
        env_profile=env_profile,
        requirements=_requirements(path, raw.get("requirements"), f"{where}.requirements"),
    )


def _parse(path: Path) -> Integration:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail(path, f"cannot read manifest: {exc}")
    if not isinstance(data, dict):
        _fail(path, "top level must be an object")
    kind = _str(path, data, "kind")
    if kind not in KINDS:
        _fail(path, f"kind must be one of {', '.join(KINDS)}")
    raw_servers = data.get("servers", [])
    if not isinstance(raw_servers, list):
        _fail(path, "servers must be a list")
    servers = tuple(_server(path, kind, s, i) for i, s in enumerate(raw_servers))
    if kind in _MCP_KINDS and not servers:
        _fail(path, f"{kind} needs at least one server")
    if kind == "native" and servers:
        _fail(path, "a native integration has no MCP servers")
    capability = _str(path, data, "capability", required=(kind == "native"))
    tools = data.get("tools", "from_server")
    if tools not in ("from_server", "from_capability"):
        tools = _str_list(path, tools, "tools")
    settings = data.get("settings", [])
    if settings != "from_capability":
        settings = _str_list(path, settings, "settings")
    if "from_capability" in (tools, settings) and not capability:
        _fail(path, "'from_capability' needs a capability")
    health = data.get("health", {"type": "none"})
    if not isinstance(health, dict) or health.get("type") not in _HEALTH_TYPES:
        _fail(path, f"health.type must be one of {', '.join(_HEALTH_TYPES)}")
    if health["type"] == "capability" and not isinstance(health.get("name"), str):
        _fail(path, "health.name is required for a capability probe")
    return Integration(
        id=_str(path, data, "id"),
        name=_str(path, data, "name"),
        description=_str(path, data, "description"),
        kind=kind,
        directory=path.parent,
        capability=capability,
        servers=servers,
        requirements=_requirements(path, data.get("requirements"), "requirements"),
        settings=settings,
        tools=tools,
        function_calling=bool(data.get("function_calling", False)),
        prompt=_str(path, data, "prompt", required=False),
        skills=_str_list(path, data.get("skills", []), "skills"),
        loadouts=_str_list(path, data.get("loadouts", []), "loadouts"),
        health=health,
        docs=_str(path, data, "docs", required=False),
        order=int(data.get("order", 100)) if isinstance(data.get("order", 100), int) else _fail(path, "order must be an integer"),
    )


def integrations_root() -> Path:
    return Path(get_app_root()) / "integrations"


_lock = threading.Lock()
_loaded: Optional[Tuple[Integration, ...]] = None


def all() -> Tuple[Integration, ...]:  # noqa: A001 - the module is the namespace
    """Every integration, in a stable order (directory name). Loaded once."""
    global _loaded
    if _loaded is None:
        with _lock:
            if _loaded is None:
                found = sorted(
                    (_parse(p) for p in integrations_root().glob(f"*/{MANIFEST_NAME}")),
                    key=lambda i: (i.order, i.id),
                )
                seen: Dict[str, Path] = {}
                for item in found:
                    # An integration may share its id with its only server
                    # ("email" / "email"); two integrations may not share any.
                    for ident in {item.id, *(s.id for s in item.servers)}:
                        if ident in seen:
                            raise IntegrationManifestError(
                                f"{item.directory}: id {ident!r} is also used by {seen[ident]}")
                        seen[ident] = item.directory
                _loaded = tuple(found)
    return _loaded


def reload() -> None:
    """Forget the cache. For tests that write their own manifests."""
    global _loaded
    with _lock:
        _loaded = None


def get(integration_id: str) -> Optional[Integration]:
    for item in all():
        if item.id == integration_id:
            return item
    return None


def _servers() -> List[Tuple[Integration, Server]]:
    return [(i, s) for i in all() for s in i.servers]


def integration_for_server(server_id: str) -> Optional[str]:
    for integration, server in _servers():
        if server.id == server_id:
            return integration.id
    return None


# ── derived lists (what the four hand-kept lists used to be) ─────────────


def builtin_mcp_specs() -> Dict[str, dict]:
    """Every built-in MCP server, keyed by server id, in manifest order."""
    specs: Dict[str, dict] = {}
    for integration, server in _servers():
        args = [a.replace("{tools}", ",".join(server.tools)) for a in server.args]
        specs[server.id] = {
            "id": server.id,
            "integration": integration.id,
            "kind": integration.kind,
            "name": server.name,
            "script": server.script,
            "command": server.command,
            "args": args,
            "env_profile": server.env_profile,
            "function_calling": integration.function_calling,
        }
    return specs


def is_builtin(server_id: str) -> bool:
    return integration_for_server(server_id) is not None


def function_calling_server_ids() -> frozenset:
    return frozenset(s.id for i, s in _servers() if i.function_calling)


def server_tools(server_id: str) -> Tuple[str, ...]:
    """The tool allow-list a built-in binary server is started with."""
    for _integration, server in _servers():
        if server.id == server_id:
            return server.tools
    return ()


# ── requirement checks ───────────────────────────────────────────────────


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _run_check(check: dict) -> Tuple[bool, str]:
    kind = check["type"]
    try:
        if kind == "env":
            ok = bool(_env(check["var"]))
            return ok, "" if ok else f"{check['var']} is not set"
        if kind == "env_flag":
            ok = _env(check["var"]).lower() in _TRUTHY
            return ok, "" if ok else f"{check['var']} is not enabled"
        if kind == "env_all":
            absent = [v for v in check["vars"] if not _env(v)]
            return not absent, ("" if not absent else f"{', '.join(absent)} not set")
        if kind == "setting":
            from src.settings import get_setting

            ok = bool(get_setting(check["key"], None))
            return ok, "" if ok else f"setting {check['key']} is empty"
        if kind == "binary":
            for name in check["names"]:
                found = shutil.which(name)
                if found:
                    return True, found
            return False, f"{' / '.join(check['names'])} not found on PATH"
        if kind == "capability":
            from src import capabilities, capabilities_builtin  # noqa: F401 - registers

            cap = capabilities.status(check["name"])
            if cap is None:
                return True, "capability not registered"
            return cap.satisfied, "" if cap.satisfied else "; ".join(u["detail"] for u in cap.unmet)
        if kind == "callable":
            module, _, attr = check["ref"].partition(":")
            result = getattr(importlib.import_module(module), attr)()
            if isinstance(result, tuple) and len(result) == 2:
                return bool(result[0]), str(result[1])
            return bool(result), ""
        if kind == "any_of":
            details = []
            for child in check["checks"]:
                ok, detail = _run_check(child)
                if ok:
                    return True, detail
                details.append(detail)
            return False, "; ".join(d for d in details if d)
        if kind == "all_of":
            for child in check["checks"]:
                ok, detail = _run_check(child)
                if not ok:
                    return False, detail
            return True, ""
    except Exception as exc:  # a probe must never break status
        return False, f"{type(exc).__name__}: {exc}"
    return False, f"unknown check {kind!r}"


def _unmet(requirements: Tuple[Requirement, ...]) -> List[dict]:
    out = []
    for req in requirements:
        ok, detail = _run_check(req.check)
        if not ok:
            out.append({"requirement": req.name, "detail": detail, "hint": req.hint})
    return out


def _server_requirements(integration: Integration, server: Server) -> Tuple[Requirement, ...]:
    return integration.requirements + server.requirements


def startable(server_id: str) -> bool:
    """False while a ``gates_start`` requirement of this server is unmet."""
    for integration, server in _servers():
        if server.id == server_id:
            gating = tuple(r for r in _server_requirements(integration, server) if r.gates_start)
            return not _unmet(gating)
    return False


# ── status ───────────────────────────────────────────────────────────────


def _manager(manager):
    if manager is not None:
        return manager
    try:
        from src.tool_utils import get_mcp_manager

        return get_mcp_manager()
    except Exception:
        return None


def _connected(manager, server_id: str) -> bool:
    if manager is None:
        return False
    try:
        return (manager.get_server_status(server_id) or {}).get("status") == "connected"
    except Exception:
        return False


def _mcp_enabled() -> bool:
    # ODYSSEUS_DISABLE_MCP is read at import by builtin_mcp; read the same name
    # here so a test or a late env change is honoured.
    return _env("ODYSSEUS_DISABLE_MCP").lower() not in _TRUTHY


def _missing(unmet: List[dict]) -> List[dict]:
    return [{"requirement": u["requirement"], "detail": u["detail"], "hint": u["hint"]} for u in unmet]


def server_status(server_id: str, manager=None) -> dict:
    """Status of one built-in server: ``enabled``, ``configured`` (every
    requirement met), ``connected`` and ``missing`` (the unmet requirements)."""
    for integration, server in _servers():
        if server.id == server_id:
            unmet = _unmet(_server_requirements(integration, server))
            return {
                "enabled": _mcp_enabled(),
                "configured": not unmet,
                "connected": _connected(_manager(manager), server_id),
                "missing": _missing(unmet),
            }
    return {"enabled": False, "configured": False, "connected": False, "missing": []}


def status(integration_id: str, manager=None) -> dict:
    """Status of an integration.

    MCP kinds: ``configured`` when its own requirements are met and at least one
    server's are; ``connected`` when any server is. ``missing`` then lists what
    the first server still lacks. Native kinds follow their capability: ``enabled``
    is the feature switch, ``configured`` the host requirements, ``connected``
    follows ``configured`` (there is no process to connect).
    """
    integration = get(integration_id)
    if integration is None:
        return {"enabled": False, "configured": False, "connected": False, "missing": []}
    if not integration.is_mcp:
        from src import capabilities, capabilities_builtin  # noqa: F401 - registers

        cap = capabilities.status(integration.capability)
        own = _unmet(integration.requirements)
        enabled = True if cap is None else cap.enabled
        return {"enabled": enabled, "configured": not own, "connected": enabled and not own,
                "missing": _missing(own)}
    shared = _unmet(integration.requirements)
    per_server = [server_status(s.id, manager) for s in integration.servers]
    configured = not shared and any(s["configured"] for s in per_server)
    missing = _missing(shared) or (per_server[0]["missing"] if not configured and per_server else [])
    return {
        "enabled": _mcp_enabled(),
        "configured": configured,
        "connected": any(s["connected"] for s in per_server),
        "missing": missing,
    }


def available_ids(manager=None) -> set:
    """Integrations that are enabled, configured and (MCP kinds) connected.

    This is what skill and loadout gating should ask: a skill that needs Penpot
    is only worth showing while Penpot tools can actually be called.
    """
    out = set()
    for integration in all():
        st = status(integration.id, manager)
        if st["enabled"] and st["configured"] and st["connected"]:
            out.add(integration.id)
    # Plugin-installed integrations: healthy means their MCP server is connected.
    if _mcp_enabled():
        mgr = _manager(manager)
        for item in plugin_integrations():
            if _connected(mgr, item.server_id):
                out.add(item.id)
    return out


def catalog_rows(manager=None) -> List[dict]:
    """One Settings > Built-in row per MCP server (GitHub read and write are two).

    ``configured`` and ``missing`` come from the requirements; ``enable`` joins
    their hints so the UI can say what to set.
    """
    rows = []
    for integration, server in _servers():
        st = server_status(server.id, manager)
        rows.append({
            "id": server.id,
            "integration": integration.id,
            "name": server.title or integration.name,
            "kind": "tool server",
            "description": server.description or integration.description,
            "configured": st["configured"],
            "missing": st["missing"],
            "enable": " ".join(m["hint"] for m in st["missing"] if m["hint"]),
        })
    return rows


# ── tools, skills, loadouts ──────────────────────────────────────────────


def integration_for_tool(tool_name: str) -> Optional[str]:
    """The integration a tool belongs to, or None for core tools.

    MCP tools resolve through their server id (``mcp__penpot_studio__x`` ->
    ``penpot``); native tools through the integration's capability tool list.
    """
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__", 2)
        if len(parts) != 3:
            return None
        return integration_for_server(parts[1]) or plugin_integration_for_server(parts[1])
    for integration in all():
        names = integration.tools
        if names == "from_capability":
            from src import capabilities, capabilities_builtin  # noqa: F401 - registers

            cap = capabilities.get(integration.capability)
            names = cap.tools if cap else ()
        if isinstance(names, (list, tuple)) and tool_name in names:
            return integration.id
    return None


def skill_dirs(integration_id: str) -> List[Path]:
    """Absolute skill directories the integration ships (existing ones only)."""
    integration = get(integration_id)
    if integration is None:
        return []
    return [p for p in ((integration.directory / rel).resolve() for rel in integration.skills) if p.is_dir()]


def loadout_templates(integration_id: str) -> List[Path]:
    """Absolute loadout template files the integration ships (existing ones only)."""
    integration = get(integration_id)
    if integration is None:
        return []
    return [p for p in ((integration.directory / rel).resolve() for rel in integration.loadouts) if p.is_file()]


# ── plugin-installed integrations (kind "plugin") ────────────────────────
#
# 2026-10-01 (plugin catalog v2). A v2 plugin an administrator installed is an
# integration too, but it lives in the data dir, not in integrations/*/. It is
# deliberately NOT part of all(): the built-in lists (is_builtin, the pinned
# function-calling set, Settings > Built-in rows) must not change because a
# user installed a package. Plugin integrations are read from the install
# records src.plugin_catalog writes and exposed through the functions below;
# available_ids() and integration_for_tool() consult them after the built-ins.


@dataclass(frozen=True)
class PluginIntegration:
    id: str  # the plugin id
    name: str
    server_id: str
    version: str = ""
    instructions: str = ""
    kind: str = "plugin"


def _plugin_records() -> list:
    try:
        from src import plugin_catalog

        return plugin_catalog.list_install_records()
    except Exception:  # a bad record must never take prompt building down
        logger.warning("plugin install records unreadable", exc_info=True)
        return []


def plugin_integrations() -> Tuple[PluginIntegration, ...]:
    """Installed plugins that created an MCP server, in plugin-id order."""
    out = []
    for rec in _plugin_records():
        server_id = rec.get("server_id")
        plugin_id = rec.get("plugin_id")
        if isinstance(server_id, str) and server_id and isinstance(plugin_id, str) and plugin_id:
            out.append(PluginIntegration(
                id=plugin_id,
                name=str(rec.get("name") or plugin_id),
                server_id=server_id,
                version=str(rec.get("version") or ""),
                instructions=str(rec.get("instructions") or ""),
            ))
    return tuple(out)


def plugin_integration_for_server(server_id: str) -> Optional[str]:
    for item in plugin_integrations():
        if item.server_id == server_id:
            return item.id
    return None


def plugin_instructions(server_id: str) -> str:
    """The prompt text an installed plugin attached to its server ('' if none).

    Untrusted like every server description; mcp_manager shows it inside the
    server's tool block only while those tools are offered.
    """
    for item in plugin_integrations():
        if item.server_id == server_id:
            return item.instructions
    return ""


def plugin_instructions_key() -> tuple:
    """A cheap value that changes when plugin instructions change (cache key)."""
    return tuple((p.server_id, p.instructions) for p in plugin_integrations() if p.instructions)
