"""Administrative CLI.

Destructive operations live here and nowhere else: the MCP surface is
read-oriented, so nothing a model says can wipe or roll back data. Deleting
requires a human at a terminal typing a confirmation word.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .config import load_config
from .database import BatchStatus, Database
from .models import SourceType
from .policy import PolicyError
from .server import ServerContext
from .server import run as run_server
from .services.import_service import ImportRefused

__all__ = ["main"]

HEARTBEAT_FILENAME = "maintenance.heartbeat"
_WIPE_CONFIRMATION = "DELETE ALL MOOD DATA"


def _emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _context(args: argparse.Namespace) -> ServerContext:
    return ServerContext.from_path(args.config)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    asyncio.run(run_server(args.config))
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    """Liveness check for Docker. Reports structure only, never mood data."""
    try:
        config = load_config(args.config)
        config.ensure_directories()
        database = Database(config.paths.database_path)
        database.initialize()
        counts = database.counts()
    except Exception as exc:
        _emit({"status": "unhealthy", "error": type(exc).__name__})
        return 1
    _emit(
        {
            "status": "ok",
            "version": __version__,
            "schema_version": database.schema_version(),
            # Row counts, not content. Deliberately no labels, notes or dates.
            "stored_entries": counts["entries"],
            "import_batches": counts["batches"],
            "entries_with_notes": counts["entries_with_notes"],
            # Never claim encryption this application does not perform.
            "database_encryption": "none (plain SQLite; use an encrypted host volume)",
            "transport": "stdio",
        }
    )
    return 0


def cmd_maintain(args: argparse.Namespace) -> int:
    """Idle container process.

    The MCP server itself is stdio and is spawned per client session. This
    process exists only so a Compose service can hold the volumes, answer the
    health check, and give an operator a place to run `exec` admin commands.
    """
    config = load_config(args.config)
    config.ensure_directories()
    Database(config.paths.database_path).initialize()
    heartbeat = config.paths.state_dir / HEARTBEAT_FILENAME
    while True:
        heartbeat.write_text(str(int(time.time())), encoding="utf-8")
        time.sleep(args.interval)


def cmd_validate(args: argparse.Namespace) -> int:
    ctx = _context(args)
    report = ctx.imports.import_file(
        args.path,
        source_type=SourceType(args.source_type),
        dry_run=True,
        mapping_name=args.mapping,
        include_notes=args.include_notes,
    )
    _emit(report.model_dump(mode="json"))
    return 0 if report.rejected_count == 0 else 2


def cmd_import(args: argparse.Namespace) -> int:
    ctx = _context(args)
    report = ctx.imports.import_file(
        args.path,
        source_type=SourceType(args.source_type),
        dry_run=False,
        mapping_name=args.mapping,
        include_notes=args.include_notes,
    )
    _emit(report.model_dump(mode="json"))
    return (
        0 if report.status in (BatchStatus.SUCCESS.value, BatchStatus.DUPLICATE_FILE.value) else 2
    )


def cmd_batches(args: argparse.Namespace) -> int:
    ctx = _context(args)
    _emit({"batches": ctx.imports.get_import_status(args.limit)})
    return 0


def cmd_files(args: argparse.Namespace) -> int:
    ctx = _context(args)
    _emit({"incoming": ctx.imports.list_available_files()})
    return 0


def cmd_mappings(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    _emit({name: mapping.describe() for name, mapping in sorted(config.mappings.items())})
    return 0


def cmd_policy(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    _emit(config.privacy.model_dump())
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    database = Database(config.paths.database_path)
    try:
        deleted = database.rollback_batch(args.batch_id)
    except KeyError:
        _emit({"error": "not_found", "message": f"No batch with id {args.batch_id}."})
        return 2
    _emit({"batch_id": args.batch_id, "deleted_entries": deleted, "status": "rolled_back"})
    return 0


def cmd_redact_notes(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    database = Database(config.paths.database_path)
    cleared = database.redact_all_notes()
    _emit(
        {
            "cleared_notes": cleared,
            # Hashes stay so re-importing the same export still deduplicates.
            "note": "Note text removed; note hashes retained for deduplication.",
        }
    )
    return 0


def cmd_wipe(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if not sys.stdin.isatty() and not args.force:
        _emit(
            {
                "error": "confirmation_required",
                "message": "Run interactively, or pass --force after reading PRIVACY.md.",
            }
        )
        return 2
    if not args.force:
        print(f"This deletes every stored mood entry in {config.paths.database_path.name}.")
        print(f"Type exactly: {_WIPE_CONFIRMATION}")
        if input("> ").strip() != _WIPE_CONFIRMATION:
            _emit({"status": "aborted"})
            return 1
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(config.paths.database_path) + suffix)
        if candidate.exists():
            candidate.unlink()
    Database(config.paths.database_path).initialize()
    _emit({"status": "wiped", "message": "Database deleted and re-initialized empty."})
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    """Re-import every file previously moved to ``processed``.

    The processed directory is the record of what was approved, so a rebuild
    never reaches outside it. Deduplication makes the operation idempotent.
    """
    config = load_config(args.config)
    if args.fresh:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(config.paths.database_path) + suffix)
            if candidate.exists():
                candidate.unlink()

    # Rebuild reads from processed/, so point the import root there for this run.
    rebuild_config = config.model_copy(
        update={
            "paths": config.paths.model_copy(update={"import_root": config.paths.processed_dir})
        }
    )
    ctx = ServerContext(rebuild_config)
    results = []
    for name in ctx.imports.list_available_files():
        try:
            report = ctx.imports.import_file(
                name,
                source_type=SourceType(args.source_type),
                dry_run=False,
                mapping_name=args.mapping,
                include_notes=args.include_notes,
            )
            results.append(
                {"file": name, "status": report.status, "inserted": report.inserted_count}
            )
        except (ImportRefused, PolicyError) as exc:
            results.append({"file": name, "status": "refused", "message": str(exc)})
    _emit({"rebuilt": results, "entries": Database(config.paths.database_path).counts()})
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lotus-mcp",
        description="Local-first MCP adapter over user-owned mood exports.",
    )
    parser.add_argument("--config", help="Path to config.yaml (default: $LOTUS_CONFIG).")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="Run the MCP server on stdio.").set_defaults(func=cmd_serve)
    sub.add_parser("health", help="Report health as JSON (used by Docker).").set_defaults(
        func=cmd_health
    )

    maintain = sub.add_parser("maintain", help="Idle container process holding the volumes.")
    maintain.add_argument("--interval", type=int, default=60)
    maintain.set_defaults(func=cmd_maintain)

    def add_import_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("path", help="Path relative to the configured import root.")
        p.add_argument(
            "--source-type",
            default=SourceType.UNKNOWN.value,
            choices=[s.value for s in SourceType],
        )
        p.add_argument("--mapping", default=None, help="Name of a configured field mapping.")
        p.add_argument(
            "--include-notes",
            action="store_true",
            help="Request note import (still requires privacy.import_notes = true).",
        )

    validate = sub.add_parser("validate", help="Dry-run a file; writes nothing, moves nothing.")
    add_import_args(validate)
    validate.set_defaults(func=cmd_validate)

    do_import = sub.add_parser("import", help="Import a file and move it to processed/.")
    add_import_args(do_import)
    do_import.set_defaults(func=cmd_import)

    batches = sub.add_parser("batches", help="List recent import batches.")
    batches.add_argument("--limit", type=int, default=20)
    batches.set_defaults(func=cmd_batches)

    sub.add_parser("files", help="List importable files in the incoming directory.").set_defaults(
        func=cmd_files
    )
    sub.add_parser("mappings", help="Show configured field mappings.").set_defaults(
        func=cmd_mappings
    )
    sub.add_parser("policy", help="Show the active privacy policy.").set_defaults(func=cmd_policy)

    rollback = sub.add_parser("rollback", help="Delete every entry from one import batch.")
    rollback.add_argument("batch_id")
    rollback.set_defaults(func=cmd_rollback)

    sub.add_parser("redact-notes", help="Remove all stored note text.").set_defaults(
        func=cmd_redact_notes
    )

    wipe = sub.add_parser("wipe", help="Delete all local mood data (interactive confirmation).")
    wipe.add_argument("--force", action="store_true", help="Skip the interactive prompt.")
    wipe.set_defaults(func=cmd_wipe)

    rebuild = sub.add_parser("rebuild", help="Re-import every file in processed/.")
    rebuild.add_argument("--fresh", action="store_true", help="Delete the database first.")
    rebuild.add_argument(
        "--source-type", default=SourceType.UNKNOWN.value, choices=[s.value for s in SourceType]
    )
    rebuild.add_argument("--mapping", default=None)
    rebuild.add_argument("--include-notes", action="store_true")
    rebuild.set_defaults(func=cmd_rebuild)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (ImportRefused, PolicyError) as exc:
        _emit({"error": type(exc).__name__, "message": str(exc)})
        return 2
    except FileNotFoundError as exc:
        _emit({"error": "FileNotFoundError", "message": str(exc)})
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
