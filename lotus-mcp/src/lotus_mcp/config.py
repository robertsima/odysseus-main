"""Configuration loading: YAML file plus a small set of env overrides.

Conservative defaults live here, not in documentation. A missing config file
yields the same locked-down policy as the shipped example.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .importers.mapping import FieldMapping, default_mappings

__all__ = ["AppConfig", "Limits", "Paths", "PrivacyPolicySettings", "load_config"]

ENV_CONFIG_PATH = "LOTUS_CONFIG"
ENV_DATA_DIR = "LOTUS_DATA_DIR"
ENV_IMPORT_ROOT = "LOTUS_IMPORT_ROOT"
ENV_LOG_LEVEL = "LOTUS_LOG_LEVEL"
ENV_TOOL_NAME_STYLE = "LOTUS_TOOL_NAME_STYLE"


class PrivacyPolicySettings(BaseModel):
    """Server-side consent boundaries.

    These are enforced in :mod:`lotus_mcp.policy`; a client argument can
    only ever narrow them, never widen them.
    """

    model_config = ConfigDict(extra="forbid")

    expose_aggregate_summaries: bool = True
    expose_emotion_labels: bool = True
    expose_raw_entries: bool = False
    expose_notes: bool = False
    include_note_themes: bool = False
    import_notes: bool = False
    allow_cross_domain_correlation: bool = False
    allow_writeback_to_source: bool = False
    allow_external_network_calls: bool = False
    max_query_days_without_confirmation: int = Field(default=90, ge=1, le=3650)
    maximum_entry_result_limit: int = Field(default=500, ge=1, le=5000)


class Limits(BaseModel):
    """Bounds applied to untrusted files before and during parsing."""

    model_config = ConfigDict(extra="forbid")

    max_file_bytes: int = Field(default=25 * 1024 * 1024, ge=1024)
    max_records_per_file: int = Field(default=100_000, ge=1)
    max_note_chars: int = Field(default=8_000, ge=1)
    max_field_chars: int = Field(default=2_000, ge=1)
    max_json_depth: int = Field(default=12, ge=2, le=100)
    max_columns: int = Field(default=200, ge=1)
    #: What to do with a note longer than ``max_note_chars``.
    oversize_note_policy: str = Field(default="reject", pattern="^(reject|truncate)$")


class Paths(BaseModel):
    """Filesystem layout. Every path is resolved once, at load time."""

    model_config = ConfigDict(extra="forbid")

    import_root: Path = Path("imports/incoming")
    processed_dir: Path = Path("imports/processed")
    failed_dir: Path = Path("imports/failed")
    database_path: Path = Path("data/mood.db")
    state_dir: Path = Path("data")

    @field_validator("*", mode="after")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        return Path(value).expanduser().resolve(strict=False)


class AppConfig(BaseModel):
    """Fully-resolved runtime configuration."""

    model_config = ConfigDict(extra="forbid")

    privacy: PrivacyPolicySettings = Field(default_factory=PrivacyPolicySettings)
    limits: Limits = Field(default_factory=Limits)
    paths: Paths = Field(default_factory=Paths)
    mappings: dict[str, FieldMapping] = Field(default_factory=default_mappings)
    default_mapping: str = "generic"
    log_level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR)$")
    #: MCP tool naming. ``dotted`` matches the documented ``mood.search_entries``
    #: names; ``underscore`` emits ``mood_search_entries`` for clients that
    #: restrict tool names to ``[A-Za-z0-9_-]``.
    tool_name_style: str = Field(default="dotted", pattern="^(dotted|underscore)$")

    @field_validator("mappings")
    @classmethod
    def _require_mappings(cls, value: dict[str, FieldMapping]) -> dict[str, FieldMapping]:
        if not value:
            raise ValueError("at least one field mapping must be configured")
        return value

    def mapping(self, name: str | None) -> FieldMapping:
        """Look up a configured mapping by name.

        Unknown names are an error rather than a silent fall-back to the
        default: sensitive fields must never be inferred from a mapping the
        operator did not choose.
        """
        key = name or self.default_mapping
        try:
            return self.mappings[key]
        except KeyError:
            known = ", ".join(sorted(self.mappings)) or "(none)"
            raise ValueError(f"Unknown mapping '{key}'. Configured mappings: {known}") from None

    def ensure_directories(self) -> None:
        for path in (
            self.paths.import_root,
            self.paths.processed_dir,
            self.paths.failed_dir,
            self.paths.state_dir,
            self.paths.database_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)


def _read_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        # safe_load never constructs arbitrary Python objects from the file.
        loaded = yaml.safe_load(handle)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("Configuration file must contain a YAML mapping at the top level.")
    return loaded


def _apply_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """Let the container override paths, log level, and MCP tool-name style.

    Privacy settings are deliberately *not* env-overridable: loosening consent
    should require editing a mounted, reviewable config file.
    """
    paths = dict(raw.get("paths") or {})
    if data_dir := os.environ.get(ENV_DATA_DIR):
        base = Path(data_dir)
        paths["database_path"] = str(base / "mood.db")
        paths["state_dir"] = str(base)
    if import_root := os.environ.get(ENV_IMPORT_ROOT):
        base = Path(import_root)
        paths["import_root"] = str(base / "incoming")
        paths["processed_dir"] = str(base / "processed")
        paths["failed_dir"] = str(base / "failed")
    if paths:
        raw["paths"] = paths
    if level := os.environ.get(ENV_LOG_LEVEL):
        raw["log_level"] = level.upper()
    if tool_name_style := os.environ.get(ENV_TOOL_NAME_STYLE):
        raw["tool_name_style"] = tool_name_style.lower()
    return raw


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load configuration from ``path``, ``$LOTUS_CONFIG``, or built-in defaults.

    A path passed explicitly by a caller is strict: a typo must not silently
    become "run with defaults". A path that merely came from the environment is
    tolerant, because the container image sets ``$LOTUS_CONFIG`` before anyone
    has mounted a config file. Falling back is safe in that direction — the
    built-in defaults are the most restrictive settings there are, so a missing
    file can only ever grant *less* access, never more.
    """
    explicit = path is not None
    candidate = path or os.environ.get(ENV_CONFIG_PATH)
    raw: dict[str, Any] = {}
    if candidate:
        config_path = Path(candidate).expanduser()
        if config_path.is_file():
            raw = _read_yaml(config_path)
        elif explicit:
            raise FileNotFoundError(f"Configuration file not found: {config_path}")
        else:
            print(
                f"lotus-mcp: no config file at {config_path}; "
                "using built-in defaults (all sensitive access disabled).",
                file=sys.stderr,
            )

    raw = _apply_env_overrides(raw)

    # Configured mappings extend the built-ins rather than replacing them, so a
    # partial config file cannot accidentally remove the generic mapping.
    merged = default_mappings()
    for name, spec in (raw.get("mappings") or {}).items():
        merged[name] = FieldMapping.model_validate(spec)
    raw["mappings"] = merged

    return AppConfig.model_validate(raw)
