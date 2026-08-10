"""The MCP stdio server.

Transport is stdio only. The server has no listening socket, no bearer token
to leak, and no HTTP surface to misconfigure — the client that spawns the
process is the only thing that can talk to it. See SECURITY.md for why the
optional HTTP transport was left out of this MVP rather than half-built.

Tool dispatch is split from transport so the whole tool surface is testable
without a client: :func:`dispatch` is an ordinary function over plain dicts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .config import AppConfig, load_config
from .database import Database
from .models import SourceType
from .policy import PolicyEngine, PolicyError, install_network_guard
from .security import install_note_safe_logging
from .services.import_service import ImportRefused, ImportService
from .services.query_service import QueryService
from .services.summary_service import GROUP_BY_CHOICES, PATTERN_GROUP_BY_CHOICES, SummaryService

__all__ = ["ServerContext", "build_tools", "dispatch", "main", "tool_name"]

logger = install_note_safe_logging(logging.getLogger("lotus_mcp"))

SERVER_NAME = "lotus"

#: Logical tool names. The wire name depends on ``config.tool_name_style``.
TOOL_IDS = (
    "get_import_status",
    "import_file",
    "search_entries",
    "summarize_period",
    "detect_low_energy_patterns",
)


def tool_name(tool_id: str, style: str) -> str:
    """``mood.search_entries`` (documented) or ``mood_search_entries``.

    Some MCP clients constrain tool names to ``[A-Za-z0-9_-]``; the underscore
    style exists for those without changing the documented default.
    """
    return f"mood.{tool_id}" if style == "dotted" else f"mood_{tool_id}"


class ServerContext:
    """Wires configuration to the service objects and records consent."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        config.ensure_directories()
        self.database = Database(config.paths.database_path)
        self.database.initialize()
        self.policy = PolicyEngine(config.privacy)
        self.network_guarded = install_network_guard(config.privacy)
        self.imports = ImportService(config, self.policy, self.database)
        self.queries = QueryService(self.policy, self.database)
        self.summaries = SummaryService(self.policy, self.database, self.queries)
        # Auditable record of the boundary the data was collected under.
        self.database.record_policy(self.policy.snapshot())

    @classmethod
    def from_path(cls, config_path: str | Path | None = None) -> ServerContext:
        return cls(load_config(config_path))


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_ISO_HINT = "ISO-8601 timestamp including a UTC offset, e.g. 2026-08-01T00:00:00-04:00"


def build_tools(style: str = "dotted") -> list[Tool]:
    name = lambda tool_id: tool_name(tool_id, style)  # noqa: E731
    return [
        Tool(
            name=name("get_import_status"),
            description=(
                "List recent mood-import batches: counts, status, sanitized filename, and "
                "mapping used. Returns no journal notes, no mood records, and no host paths."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name=name("import_file"),
            description=(
                "Validate or import one user-provided CSV/JSON mood export from the approved "
                "import directory. Defaults to a dry run that writes nothing and moves nothing. "
                "'path' is relative to the configured import root; absolute paths, '..' and "
                "symlinks pointing outside the root are rejected."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Path relative to the import root, e.g. 'moods-2026-08.csv'."
                        ),
                    },
                    "source_type": {
                        "type": "string",
                        "enum": [s.value for s in SourceType],
                        "default": SourceType.UNKNOWN.value,
                        "description": (
                            "Provenance label. Only how_we_feel_export, manual_csv, manual_json "
                            "and unknown have a tested parser; the health-export values are "
                            "accepted as labels but refused at import time."
                        ),
                    },
                    "dry_run": {"type": "boolean", "default": True},
                    "mapping_name": {"type": "string"},
                    "include_notes": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Request note import. Honoured only if server policy also allows it; "
                            "otherwise the call is refused."
                        ),
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name=name("search_entries"),
            description=(
                "Return individual mood entries in a bounded date range. Disabled by default: "
                "under the shipped policy this returns a policy error and the caller should use "
                "summarize_period instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": _ISO_HINT},
                    "end": {"type": "string", "description": _ISO_HINT},
                    "emotion_labels": {"type": "array", "items": {"type": "string"}},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "include_notes": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
                    "cursor": {"type": "string"},
                    "confirm_extended_range": {
                        "type": "boolean",
                        "default": False,
                        "description": "Acknowledge a range wider than the configured day limit.",
                    },
                },
                "required": ["start", "end"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name=name("summarize_period"),
            description=(
                "Aggregate mood statistics over a date range: entry counts, emotion-label counts, "
                "and average valence/energy/intensity per time bucket, with explicit "
                "missing-data cautions. Never returns note text and never diagnoses."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": _ISO_HINT},
                    "end": {"type": "string", "description": _ISO_HINT},
                    "group_by": {
                        "type": "string",
                        "enum": list(GROUP_BY_CHOICES),
                        "default": "day",
                    },
                    "include_emotion_counts": {"type": "boolean", "default": True},
                    "include_note_themes": {"type": "boolean", "default": False},
                    "confirm_extended_range": {"type": "boolean", "default": False},
                },
                "required": ["start", "end"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name=name("detect_low_energy_patterns"),
            description=(
                "Report which time buckets contain the most low-energy check-ins, together with "
                "the sample size behind each figure. Observational only: it describes what the "
                "imported entries contain and makes no clinical or psychological claim."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "lookback_days": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 365,
                        "default": 30,
                    },
                    "energy_threshold": {"type": "number", "default": 0.3},
                    "valence_threshold": {"type": "number", "default": -0.3},
                    "minimum_entries": {"type": "integer", "minimum": 1, "default": 3},
                    "group_by": {"type": "string", "enum": list(PATTERN_GROUP_BY_CHOICES)},
                    "confirm_extended_range": {"type": "boolean", "default": False},
                },
                "additionalProperties": False,
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _parse_timestamp(raw: Any, field: str) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{field} is required and must be an ISO-8601 timestamp.")
    text = raw.strip()
    text = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{field} is not a valid ISO-8601 timestamp.") from None
    if parsed.tzinfo is None:
        # Same rule as the importer: a timezone is never guessed.
        raise ValueError(f"{field} must include a UTC offset (e.g. +00:00).")
    return parsed


def _string_list(raw: Any, field: str) -> list[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"{field} must be an array of strings.")
    return raw


def dispatch(ctx: ServerContext, tool_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run one tool by logical id. Raises only ValueError/PolicyError/ImportRefused."""
    args = arguments or {}

    if tool_id == "get_import_status":
        return {"batches": ctx.imports.get_import_status(int(args.get("limit", 20) or 20))}

    if tool_id == "import_file":
        raw_type = args.get("source_type", SourceType.UNKNOWN.value)
        try:
            source_type = SourceType(raw_type)
        except ValueError:
            raise ValueError(
                f"Unknown source_type '{raw_type}'. Allowed: "
                + ", ".join(s.value for s in SourceType)
            ) from None
        report = ctx.imports.import_file(
            args.get("path", ""),
            source_type=source_type,
            dry_run=bool(args.get("dry_run", True)),
            mapping_name=args.get("mapping_name"),
            include_notes=bool(args.get("include_notes", False)),
        )
        return report.model_dump(mode="json")

    if tool_id == "search_entries":
        return ctx.queries.search_entries(
            start=_parse_timestamp(args.get("start"), "start"),
            end=_parse_timestamp(args.get("end"), "end"),
            emotion_labels=_string_list(args.get("emotion_labels"), "emotion_labels"),
            tags=_string_list(args.get("tags"), "tags"),
            include_notes=bool(args.get("include_notes", False)),
            limit=args.get("limit"),
            cursor=args.get("cursor"),
            confirm_extended_range=bool(args.get("confirm_extended_range", False)),
        )

    if tool_id == "summarize_period":
        return ctx.summaries.summarize_period(
            start=_parse_timestamp(args.get("start"), "start"),
            end=_parse_timestamp(args.get("end"), "end"),
            group_by=str(args.get("group_by", "day")),
            include_emotion_counts=bool(args.get("include_emotion_counts", True)),
            include_note_themes=bool(args.get("include_note_themes", False)),
            confirm_extended_range=bool(args.get("confirm_extended_range", False)),
        )

    if tool_id == "detect_low_energy_patterns":
        return ctx.summaries.detect_low_energy_patterns(
            lookback_days=int(args.get("lookback_days", 30) or 30),
            energy_threshold=float(args.get("energy_threshold", 0.3)),
            valence_threshold=float(args.get("valence_threshold", -0.3)),
            minimum_entries=int(args.get("minimum_entries", 3) or 3),
            group_by=args.get("group_by"),
            confirm_extended_range=bool(args.get("confirm_extended_range", False)),
        )

    raise ValueError(f"Unknown tool: {tool_id}")


def _error(kind: str, message: str) -> dict[str, Any]:
    return {"error": kind, "message": message}


def call_tool_sync(ctx: ServerContext, wire_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Dispatch by wire name, converting every failure into a safe payload.

    Exceptions never escape to the transport: an unexpected error is logged by
    type only and reported as a fixed phrase, so an internal message can never
    carry a fragment of a note or a host path to the client.
    """
    style = ctx.config.tool_name_style
    for tool_id in TOOL_IDS:
        if wire_name in (tool_name(tool_id, style), f"mood.{tool_id}", f"mood_{tool_id}"):
            break
    else:
        return _error("unknown_tool", f"Unknown tool: {wire_name}")

    try:
        return dispatch(ctx, tool_id, arguments)
    except PolicyError as exc:
        return _error("policy_denied", str(exc))
    except ImportRefused as exc:
        return _error("import_refused", str(exc))
    except ValueError as exc:
        return _error("invalid_request", str(exc))
    except Exception as exc:
        logger.error("Tool %s failed: %s", tool_id, type(exc).__name__)
        return _error(
            "internal_error", "The request failed. See the server log for the error type."
        )


def build_server(ctx: ServerContext) -> Server:
    server = Server(SERVER_NAME)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return build_tools(ctx.config.tool_name_style)

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        payload = call_tool_sync(ctx, name, arguments or {})
        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))]

    return server


async def run(config_path: str | Path | None = None) -> None:
    ctx = ServerContext.from_path(config_path)
    logging.basicConfig(
        level=getattr(logging, ctx.config.log_level, logging.INFO),
        # stdout is the MCP channel; logs must never be written to it.
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info(
        "lotus-mcp ready (raw_entries=%s, notes=%s, network_guard=%s)",
        ctx.config.privacy.expose_raw_entries,
        ctx.config.privacy.expose_notes,
        ctx.network_guarded,
    )
    server = build_server(ctx)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
