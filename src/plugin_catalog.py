"""Declarative plugin manifests: capability bundles and user-added integrations.

Plugins are data, not executable packages.  A v1 manifest may reference skills,
MCP server IDs, tools and models already configured by the administrator; it
cannot run installers, register MCP processes, or import Python/JavaScript.

Schema v2 (2026-10-01, website/architecture-integrations-2026-10-01.md phase 4)
adds an optional ``integration`` block so a plugin can be the package format
for a user-added integration: an MCP server spec, prompt text for it, skills
and loadout templates.  The safety stance of v1 is unchanged.  Nothing here
runs: validation only checks shape and size.  An administrator installs the
package explicitly (routes/plugin_routes.py), sees the stdio command verbatim
first, and types any secret at install time.  A manifest names its environment
variables and never carries their values, so a published package cannot ship a
credential and a stolen manifest cannot leak one.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

from core.atomic_io import atomic_write_json
from src.constants import DATA_DIR

PLUGIN_DIR = Path(DATA_DIR) / "plugins"
MAX_MANIFEST_BYTES = 64 * 1024  # v1; unchanged
MAX_MANIFEST_BYTES_V2 = 256 * 1024  # skills and loadouts live inside the manifest
SCHEMA_VERSIONS = (1, 2)
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
_TOP_KEYS = {"schema_version", "id", "name", "version", "description", "capabilities", "integration"}
_CAP_KEYS = {"skills", "mcp_servers", "tools", "models"}
_INTEGRATION_KEYS = {"name", "mcp_server", "instructions", "skills", "loadout_templates"}
_SERVER_KEYS = {"name", "transport", "command", "args", "url", "env"}
_ENV_KEYS = {"name", "description", "required"}
TRANSPORTS = ("stdio", "http", "sse")
MAX_INSTRUCTIONS_CHARS = 4096
MAX_SKILLS = 10
MAX_SKILL_CHARS = 32 * 1024
MAX_TEMPLATES = 10
MAX_ENV_VARS = 20
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# Upper-case names only: a server's own settings look like API_KEY, and
# case-folded duplicates (http_proxy vs HTTP_PROXY) stay out.
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Variables that change how the host runs code, finds code, trusts TLS or routes
# traffic, rather than configure a server. An admin can still type them into
# the MCP Settings form by hand; a package does not get to ask for them. Whole
# families are matched by prefix: the first list (2026-10-01) named eight
# variables and a security review found BASH_ENV, JAVA_TOOL_OPTIONS, PERL5OPT,
# GIT_SSH_COMMAND and NPM_CONFIG_* still open.
_FORBIDDEN_ENV = {
    "PATH", "HOME", "SHELL", "ENV", "BASH_ENV", "IFS", "PS4", "PROMPT_COMMAND", "CDPATH",
    "TMPDIR", "TEMP", "TMP", "EDITOR", "VISUAL", "PAGER", "BROWSER",
    "CLASSPATH", "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
}
_FORBIDDEN_ENV_PREFIXES = (
    "LD_", "DYLD_", "PYTHON", "NODE_", "NPM_CONFIG_", "NPX_", "PIP_", "UV_", "PERL", "RUBY",
    "GEM_", "GIT_", "BUN_", "DENO_", "JAVA", "DOCKER_", "ODYSSEUS_",
)


def _forbidden_env_name(name: str) -> bool:
    upper = name.upper()
    return upper in _FORBIDDEN_ENV or upper.startswith(_FORBIDDEN_ENV_PREFIXES)
_TEMPLATE_FORMAT = "agamemnon-agent-profiles"


class PluginManifestError(ValueError):
    pass


def _names(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise PluginManifestError(f"capabilities.{field} must be a list of non-empty names")
    names = sorted({v.strip() for v in value})
    if len(names) > 300 or any(len(v) > 300 for v in names):
        raise PluginManifestError(f"capabilities.{field} is too large")
    return names


def validate_manifest(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PluginManifestError("manifest must be a JSON object")
    unknown = set(raw) - _TOP_KEYS
    if unknown:
        raise PluginManifestError(f"unknown manifest field(s): {', '.join(sorted(unknown))}")
    schema = raw.get("schema_version")
    if type(schema) is not int or schema not in SCHEMA_VERSIONS:
        raise PluginManifestError("schema_version must be 1 or 2")
    if "integration" in raw and schema != 2:
        raise PluginManifestError("integration requires schema_version 2")
    if not isinstance(raw.get("id"), str):
        raise PluginManifestError("id must be a string")
    plugin_id = raw["id"].strip()
    if not _ID_RE.fullmatch(plugin_id):
        raise PluginManifestError("id must be 1-40 lowercase letters, digits, dots, dashes or underscores")
    if not isinstance(raw.get("name"), str) or not isinstance(raw.get("version"), str):
        raise PluginManifestError("name and version must be strings")
    name = raw["name"].strip()
    version = raw["version"].strip()
    if not name or len(name) > 100:
        raise PluginManifestError("name must be 1-100 characters")
    if not version or len(version) > 40:
        raise PluginManifestError("version must be 1-40 characters")
    capabilities = raw.get("capabilities")
    if not isinstance(capabilities, dict):
        raise PluginManifestError("capabilities must be an object")
    unknown_caps = set(capabilities) - _CAP_KEYS
    if unknown_caps:
        raise PluginManifestError(f"unknown capability field(s): {', '.join(sorted(unknown_caps))}")
    out = {
        "schema_version": schema,
        "id": plugin_id,
        "name": name,
        "version": version,
        "description": _description(raw.get("description")),
        "capabilities": {key: _names(capabilities.get(key), key) for key in sorted(_CAP_KEYS)},
    }
    if schema == 2 and raw.get("integration") is not None:
        out["integration"] = _integration(raw["integration"])
    return out


def _text(value: Any, label: str, limit: int, *, multiline: bool = False) -> str:
    if not isinstance(value, str):
        raise PluginManifestError(f"{label} must be a string")
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    if _CONTROL.search(text) or (not multiline and "\n" in text):
        raise PluginManifestError(f"{label} contains control characters")
    text = text.strip()
    if len(text) > limit:
        raise PluginManifestError(f"{label} must be at most {limit} characters")
    return text


def _env_vars(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict):
        raise PluginManifestError(
            "mcp_server.env must be a list of variable names with descriptions, never values")
    if not isinstance(value, list) or len(value) > MAX_ENV_VARS:
        raise PluginManifestError(f"mcp_server.env must be a list of at most {MAX_ENV_VARS} entries")
    seen, out = set(), []
    for item in value:
        if not isinstance(item, dict) or set(item) - _ENV_KEYS:
            raise PluginManifestError(
                "each mcp_server.env entry is {name, description, required}; values are entered at install")
        name = item.get("name")
        if not isinstance(name, str) or not _ENV_NAME_RE.fullmatch(name):
            raise PluginManifestError("mcp_server.env names must be upper-case variable names such as API_KEY")
        if _forbidden_env_name(name):
            raise PluginManifestError(f"mcp_server.env may not set {name}: it changes how code runs or connects")
        if name in seen:
            raise PluginManifestError(f"mcp_server.env lists {name} twice")
        seen.add(name)
        required = item.get("required", True)
        if not isinstance(required, bool):
            raise PluginManifestError("mcp_server.env required must be true or false")
        desc = _text(item.get("description", ""), f"env {name} description", 200)
        out.append({"name": name, "description": desc, "required": required})
    return out


def _mcp_server(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PluginManifestError("integration.mcp_server must be an object")
    unknown = set(raw) - _SERVER_KEYS
    if unknown:
        raise PluginManifestError(f"unknown mcp_server field(s): {', '.join(sorted(unknown))}")
    transport = raw.get("transport")
    if transport not in TRANSPORTS:
        raise PluginManifestError(f"mcp_server.transport must be one of {', '.join(TRANSPORTS)}")
    name = _text(raw.get("name", ""), "mcp_server.name", 100)
    if not name:
        raise PluginManifestError("mcp_server.name is required")
    out: dict[str, Any] = {"name": name, "transport": transport, "env": _env_vars(raw.get("env"))}
    if transport == "stdio":
        if raw.get("url"):
            raise PluginManifestError("an stdio mcp_server takes command and args, not url")
        command = _text(raw.get("command", ""), "mcp_server.command", 500)
        if not command:
            raise PluginManifestError("mcp_server.command is required for stdio")
        args = raw.get("args", [])
        if not isinstance(args, list) or len(args) > 50:
            raise PluginManifestError("mcp_server.args must be a list of at most 50 strings")
        out["command"] = command
        out["args"] = [_text(a, "mcp_server.args entry", 1000) for a in args]
    else:
        if raw.get("command") or raw.get("args"):
            raise PluginManifestError(f"a {transport} mcp_server takes url, not command or args")
        url = _text(raw.get("url", ""), "mcp_server.url", 500)
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise PluginManifestError("mcp_server.url must be an http(s) URL")
        if parts.username or parts.password:
            raise PluginManifestError("mcp_server.url must not contain credentials")
        out["url"] = url
    return out


def _skills(raw: Any) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_SKILLS:
        raise PluginManifestError(f"integration.skills must be a list of at most {MAX_SKILLS} skills")
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"name", "content"}:
            raise PluginManifestError("each skill is {name, content}")
        name = item["name"]
        if not isinstance(name, str) or not _SKILL_NAME_RE.fullmatch(name):
            raise PluginManifestError("skill names are 1-64 lowercase letters, digits and dashes")
        if name in seen:
            raise PluginManifestError(f"skill {name!r} is listed twice")
        seen.add(name)
        content = item["content"]
        if not isinstance(content, str) or not content.strip():
            raise PluginManifestError(f"skill {name!r} needs SKILL.md content")
        if len(content) > MAX_SKILL_CHARS:
            raise PluginManifestError(f"skill {name!r} is larger than {MAX_SKILL_CHARS} characters")
        out.append({"name": name, "content": content})
    return out


def _templates(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_TEMPLATES:
        raise PluginManifestError(f"integration.loadout_templates must be a list of at most {MAX_TEMPLATES}")
    for doc in raw:
        if (not isinstance(doc, dict) or doc.get("format") not in (_TEMPLATE_FORMAT, "odysseus-agent-profiles")
                or doc.get("version") != 1 or not isinstance(doc.get("profiles"), list)):
            raise PluginManifestError(
                f"each loadout template must be an {_TEMPLATE_FORMAT} version 1 document")
    return raw


def _integration(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PluginManifestError("integration must be an object")
    unknown = set(raw) - _INTEGRATION_KEYS
    if unknown:
        raise PluginManifestError(f"unknown integration field(s): {', '.join(sorted(unknown))}")
    name = _text(raw.get("name", ""), "integration.name", 100)
    if not name:
        raise PluginManifestError("integration.name is required")
    out: dict[str, Any] = {
        "name": name,
        "instructions": _text(raw.get("instructions", ""), "integration.instructions",
                              MAX_INSTRUCTIONS_CHARS, multiline=True),
        "skills": _skills(raw.get("skills")),
        "loadout_templates": _templates(raw.get("loadout_templates")),
    }
    if raw.get("mcp_server") is not None:
        out["mcp_server"] = _mcp_server(raw["mcp_server"])
    elif out["instructions"]:
        # Instructions are shown beside a server's tools; without a server
        # there is nothing for them to attach to.
        raise PluginManifestError("integration.instructions need an mcp_server to attach to")
    return out


class PluginCatalog:
    def __init__(self, root: str | Path | None = None):
        # Resolved per call, not at def time, so tests (and a relocated data
        # dir) can point PLUGIN_DIR somewhere else.
        self.root = Path(root if root is not None else PLUGIN_DIR)

    def list(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        manifests = []
        for path in sorted(self.root.glob("*.json")):
            if path.is_symlink():
                raise PluginManifestError(f"{path.name}: symbolic links are not allowed")
            manifests.append(self._read(path))
        return manifests

    def get(self, plugin_id: str) -> dict[str, Any] | None:
        if not _ID_RE.fullmatch(str(plugin_id or "")):
            return None
        path = self.root / f"{plugin_id}.json"
        if path.is_symlink():
            raise PluginManifestError(f"{path.name}: symbolic links are not allowed")
        return self._read(path) if path.is_file() else None

    def save(self, raw: Any) -> dict[str, Any]:
        manifest = validate_manifest(raw)
        encoded = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        limit = MAX_MANIFEST_BYTES_V2 if manifest["schema_version"] == 2 else MAX_MANIFEST_BYTES
        if len(encoded) > limit:
            raise PluginManifestError("manifest is too large")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{manifest['id']}.json"
        if path.is_symlink():
            raise PluginManifestError("refusing to overwrite a symbolic link")
        atomic_write_json(str(path), manifest, indent=2)
        return manifest

    def delete(self, plugin_id: str) -> bool:
        if not _ID_RE.fullmatch(str(plugin_id or "")):
            raise PluginManifestError("invalid plugin id")
        path = self.root / f"{plugin_id}.json"
        if path.is_symlink():
            raise PluginManifestError("refusing to delete a symbolic link")
        if not path.is_file():
            return False
        path.unlink()
        return True

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        size = path.stat().st_size
        if size > MAX_MANIFEST_BYTES_V2:
            raise PluginManifestError(f"{path.name}: manifest is too large")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PluginManifestError(f"{path.name}: invalid JSON") from exc
        if size > MAX_MANIFEST_BYTES and not (isinstance(raw, dict) and raw.get("schema_version") == 2):
            raise PluginManifestError(f"{path.name}: manifest is too large")
        manifest = validate_manifest(raw)
        if path.stem != manifest["id"]:
            raise PluginManifestError(f"{path.name}: filename must match manifest id")
        return manifest

    # Install records (see below) for this catalog's root.
    def install_record(self, plugin_id: str) -> dict[str, Any] | None:
        return read_install_record(plugin_id, self.root)

    def write_install_record(self, record: dict[str, Any]) -> None:
        write_install_record(record, self.root)

    def delete_install_record(self, plugin_id: str) -> bool:
        return delete_install_record(plugin_id, self.root)


def compile_capabilities(manifests: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Union manifest references.  This function deliberately has no effects."""
    return {
        key: sorted({name for manifest in manifests for name in manifest["capabilities"][key]})
        for key in sorted(_CAP_KEYS)
    }


def _description(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PluginManifestError("description must be a string")
    if len(value) > 1000:
        raise PluginManifestError("description must be at most 1000 characters")
    return value.strip()


# ── install records ──────────────────────────────────────────────────────
#
# What an install created, so uninstall removes exactly that and nothing the
# admin made by hand.  Records live in ``<plugins>/installed/<id>.json``; the
# catalog's ``*.json`` glob does not descend there.  A record never holds env
# values (those go to the MCP server row, as for a hand-added server).


def _records_dir(root: str | Path | None = None) -> Path:
    return Path(root if root is not None else PLUGIN_DIR) / "installed"


def read_install_record(plugin_id: str, root: str | Path | None = None) -> dict[str, Any] | None:
    if not _ID_RE.fullmatch(str(plugin_id or "")):
        return None
    path = _records_dir(root) / f"{plugin_id}.json"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def list_install_records(root: str | Path | None = None) -> list[dict[str, Any]]:
    directory = _records_dir(root)
    if not directory.is_dir():
        return []
    records = []
    for path in sorted(directory.glob("*.json")):
        record = read_install_record(path.stem, root)
        if record:
            records.append(record)
    return records


def write_install_record(record: dict[str, Any], root: str | Path | None = None) -> None:
    plugin_id = str(record.get("plugin_id") or "")
    if not _ID_RE.fullmatch(plugin_id):
        raise PluginManifestError("invalid plugin id")
    directory = _records_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    body = {**record, "installed_at": record.get("installed_at") or int(time.time())}
    atomic_write_json(str(directory / f"{plugin_id}.json"), body, indent=2)


def delete_install_record(plugin_id: str, root: str | Path | None = None) -> bool:
    if not _ID_RE.fullmatch(str(plugin_id or "")):
        return False
    path = _records_dir(root) / f"{plugin_id}.json"
    if path.is_symlink() or not path.is_file():
        return False
    path.unlink()
    return True
