"""Declarative plugin manifests for grouping existing Odysseus capabilities.

Plugins are data, not executable packages.  A manifest may reference skills,
MCP server IDs, tools and models already configured by the administrator; it
cannot run installers, register MCP processes, or import Python/JavaScript.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.atomic_io import atomic_write_json
from src.constants import DATA_DIR

PLUGIN_DIR = Path(DATA_DIR) / "plugins"
MAX_MANIFEST_BYTES = 64 * 1024
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
_TOP_KEYS = {"schema_version", "id", "name", "version", "description", "capabilities"}
_CAP_KEYS = {"skills", "mcp_servers", "tools", "models"}


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
    if type(raw.get("schema_version")) is not int or raw.get("schema_version") != 1:
        raise PluginManifestError("schema_version must be 1")
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
    return {
        "schema_version": 1,
        "id": plugin_id,
        "name": name,
        "version": version,
        "description": _description(raw.get("description")),
        "capabilities": {key: _names(capabilities.get(key), key) for key in sorted(_CAP_KEYS)},
    }


class PluginCatalog:
    def __init__(self, root: str | Path = PLUGIN_DIR):
        self.root = Path(root)

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
        if len(encoded) > MAX_MANIFEST_BYTES:
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
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise PluginManifestError(f"{path.name}: manifest is too large")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PluginManifestError(f"{path.name}: invalid JSON") from exc
        manifest = validate_manifest(raw)
        if path.stem != manifest["id"]:
            raise PluginManifestError(f"{path.name}: filename must match manifest id")
        return manifest


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
