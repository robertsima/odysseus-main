"""Import orchestration.

Ordering matters here and is deliberate:

1. Everything that can refuse the request happens before any mutation.
2. Deduplication runs *inside* the write transaction, so a concurrent import
   cannot slip a duplicate past a pre-computed count.
3. The database commits before the source file moves. If the move then fails,
   the data is safely stored and the file stays in ``incoming`` — where a
   re-import is recognised by file hash instead of duplicating entries.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .. import PARSER_VERSION
from ..config import AppConfig
from ..database import BatchStatus, Database
from ..importers.base import ParsedRecord, ParseError
from ..importers.csv_importer import parse_csv
from ..importers.json_importer import parse_json
from ..importers.mapping import RowNormalizer
from ..models import SUPPORTED_SOURCE_TYPES, MoodEntry, SourceType, ValidationProblem
from ..policy import PolicyEngine
from ..security import PathSecurityError, hash_file, resolve_within_root, sanitize_filename

__all__ = ["ImportRefused", "ImportReport", "ImportService"]

logger = logging.getLogger(__name__)

#: Only these extensions are ever opened, regardless of declared source type.
_PARSERS = {".csv": parse_csv, ".json": parse_json}

#: How many per-record problems to echo back. Enough to fix a file, few enough
#: that a hostile file cannot use the response as an amplifier.
MAX_REPORTED_PROBLEMS = 20


class ImportRefused(Exception):
    """The request was rejected before anything was read or written."""


class ImportReport(BaseModel):
    """Client-safe outcome of an import attempt.

    Contains no note text, no absolute host path, no stack trace, and no raw
    row content — only counts, structural field names, and fixed phrases.
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: str | None = None
    status: str
    dry_run: bool
    filename: str
    source: str
    source_type: str
    mapping_name: str
    parser_version: str = PARSER_VERSION
    record_count: int = 0
    inserted_count: int = 0
    duplicate_count: int = 0
    rejected_count: int = 0
    notes_imported: bool = False
    resolved_columns: dict[str, str] = Field(default_factory=dict)
    unmapped_columns: list[str] = Field(default_factory=list)
    problems: list[ValidationProblem] = Field(default_factory=list)
    problems_truncated: bool = False
    file_disposition: str = "unchanged"
    message: str = ""


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ImportService:
    def __init__(self, config: AppConfig, policy: PolicyEngine, database: Database) -> None:
        self.config = config
        self.policy = policy
        self.db = database

    # ---- pre-flight --------------------------------------------------------

    def _resolve_source_file(self, relative_path: str) -> Path:
        """Canonicalize a client path and prove it is a regular file in-root."""
        try:
            path = resolve_within_root(self.config.paths.import_root, relative_path)
        except PathSecurityError as exc:
            raise ImportRefused(str(exc)) from None

        try:
            info = path.stat()
        except OSError:
            # Deliberately does not echo the resolved absolute path.
            raise ImportRefused(
                f"No readable file named '{sanitize_filename(relative_path)}' in the import root."
            ) from None
        if not stat.S_ISREG(info.st_mode):
            raise ImportRefused("Only regular files can be imported.")
        if info.st_size == 0:
            raise ImportRefused("File is empty.")
        if info.st_size > self.config.limits.max_file_bytes:
            raise ImportRefused(
                f"File is larger than the {self.config.limits.max_file_bytes} byte import limit."
            )
        if path.suffix.lower() not in _PARSERS:
            raise ImportRefused(
                f"Unsupported file type '{path.suffix or '(none)'}'. Supported: "
                + ", ".join(sorted(_PARSERS))
            )
        return path

    @staticmethod
    def _check_source_type(source_type: SourceType) -> None:
        if source_type not in SUPPORTED_SOURCE_TYPES:
            raise ImportRefused(
                f"No verified parser exists for source_type '{source_type.value}'. This adapter "
                "does not guess at an unseen schema. Convert the data to a generic CSV/JSON file "
                "and import it as manual_csv or manual_json."
            )

    # ---- main entry point --------------------------------------------------

    def import_file(
        self,
        relative_path: str,
        *,
        source_type: SourceType = SourceType.UNKNOWN,
        dry_run: bool = True,
        mapping_name: str | None = None,
        include_notes: bool = False,
    ) -> ImportReport:
        path = self._resolve_source_file(relative_path)
        self._check_source_type(source_type)

        try:
            mapping = self.config.mapping(mapping_name)
        except ValueError as exc:
            raise ImportRefused(str(exc)) from None

        # Raises PolicyError when the client asked for notes the server forbids.
        import_notes = self.policy.resolve_import_notes(include_notes)

        safe_name = sanitize_filename(path.name)
        base = {
            "dry_run": dry_run,
            "filename": safe_name,
            "source": mapping.source_label,
            "source_type": source_type.value,
            "mapping_name": mapping_name or self.config.default_mapping,
            "notes_imported": import_notes,
        }

        file_hash = hash_file(path)
        file_bytes = path.stat().st_size

        duplicate_batch = self.db.find_batch_by_file_hash(file_hash)
        if duplicate_batch is not None:
            return self._handle_duplicate_file(path, base, duplicate_batch, dry_run)

        # ---- parse -----------------------------------------------------
        parser = _PARSERS[path.suffix.lower()]
        try:
            columns, records = parser(path, limits=self.config.limits)
        except ParseError as exc:
            return self._fail_validation(path, base, file_hash, file_bytes, str(exc), dry_run)
        except OSError:
            raise ImportRefused("File could not be read.") from None

        normalizer = RowNormalizer(mapping, self.config.limits, import_notes=import_notes)
        resolved = normalizer.resolve_columns(columns)
        unmapped = normalizer.unmapped_columns(columns)

        if "occurred_at" not in resolved and "date" not in resolved:
            wanted = ", ".join(
                mapping.aliases.get("occurred_at", []) + mapping.aliases.get("date", [])
            )
            return self._fail_validation(
                path,
                base,
                file_hash,
                file_bytes,
                f"No timestamp column found. Expected one of: {wanted}.",
                dry_run,
            )

        entries, problems = self._normalize_all(records, normalizer, resolved, unmapped)

        report_base = dict(
            base,
            record_count=len(records),
            rejected_count=len(problems),
            resolved_columns=resolved,
            unmapped_columns=unmapped,
            problems=problems[:MAX_REPORTED_PROBLEMS],
            problems_truncated=len(problems) > MAX_REPORTED_PROBLEMS,
        )

        if dry_run:
            # Deduplication is previewed read-only; nothing is written and the
            # file is not moved.
            unique, in_file_duplicates = self._dedupe_within_file(entries)
            conn = self.db.connect()
            try:
                already_stored = self._count_already_stored(conn, unique)
            finally:
                conn.close()
            return ImportReport(
                **report_base,
                status="dry_run",
                inserted_count=len(unique) - already_stored,
                duplicate_count=in_file_duplicates + already_stored,
                file_disposition="unchanged",
                message="Dry run: nothing was written and the file was not moved.",
            )

        return self._commit(path, report_base, entries, file_hash, file_bytes)

    # ---- helpers -----------------------------------------------------------

    def _normalize_all(
        self,
        records: list[ParsedRecord],
        normalizer: RowNormalizer,
        resolved: dict[str, str],
        unmapped: list[str],
    ) -> tuple[list[MoodEntry], list[ValidationProblem]]:
        entries: list[MoodEntry] = []
        problems: list[ValidationProblem] = []
        for record in records:
            entry, problem = normalizer.normalize(record, resolved, unmapped)
            if entry is not None:
                entries.append(entry)
            elif problem is not None:
                problems.append(problem)
        return entries, problems

    @staticmethod
    def _dedupe_within_file(entries: list[MoodEntry]) -> tuple[list[MoodEntry], int]:
        """Collapse repeats inside a single file before touching the database."""
        seen_fingerprints: set[str] = set()
        seen_source_ids: set[tuple[str, str]] = set()
        unique: list[MoodEntry] = []
        duplicates = 0
        for entry in entries:
            if entry.source_record_id:
                key = (entry.source, entry.source_record_id)
                if key in seen_source_ids:
                    duplicates += 1
                    continue
                seen_source_ids.add(key)
            fingerprint = entry.fingerprint()
            if fingerprint in seen_fingerprints:
                duplicates += 1
                continue
            seen_fingerprints.add(fingerprint)
            unique.append(entry)
        return unique, duplicates

    def _count_already_stored(self, conn: sqlite3.Connection, entries: list[MoodEntry]) -> int:
        return len(entries) - len(self._filter_new(conn, entries))

    def _filter_new(self, conn: sqlite3.Connection, entries: list[MoodEntry]) -> list[MoodEntry]:
        """Drop entries the database already holds.

        A source record id wins when present (it survives edits to the note or
        the label); otherwise the content fingerprint decides.
        """
        by_source: dict[str, list[str]] = {}
        for entry in entries:
            if entry.source_record_id:
                by_source.setdefault(entry.source, []).append(entry.source_record_id)
        known_ids: set[tuple[str, str]] = set()
        for source, ids in by_source.items():
            for found in Database.existing_source_ids(conn, source, ids):
                known_ids.add((source, found))

        fingerprints = [e.fingerprint() for e in entries]
        known_fingerprints = Database.existing_fingerprints(conn, fingerprints)

        fresh: list[MoodEntry] = []
        for entry, fingerprint in zip(entries, fingerprints, strict=True):
            if entry.source_record_id and (entry.source, entry.source_record_id) in known_ids:
                continue
            if fingerprint in known_fingerprints:
                continue
            fresh.append(entry)
        return fresh

    def _commit(
        self,
        path: Path,
        report_base: dict[str, Any],
        entries: list[MoodEntry],
        file_hash: str,
        file_bytes: int,
    ) -> ImportReport:
        batch_id = uuid.uuid4().hex
        try:
            with self.db.transaction() as conn:
                unique, in_file_duplicates = self._dedupe_within_file(entries)
                fresh = self._filter_new(conn, unique)
                duplicate_count = in_file_duplicates + (len(unique) - len(fresh))

                Database.insert_batch(
                    conn,
                    {
                        "id": batch_id,
                        "source": report_base["source"],
                        "source_type": report_base["source_type"],
                        "filename": report_base["filename"],
                        "file_hash": file_hash,
                        "file_bytes": file_bytes,
                        "imported_at": _utc_now_iso(),
                        "record_count": report_base["record_count"],
                        "inserted_count": len(fresh),
                        "duplicate_count": duplicate_count,
                        "rejected_count": report_base["rejected_count"],
                        "status": BatchStatus.SUCCESS.value,
                        "error_message": None,
                        "parser_version": PARSER_VERSION,
                        "mapping_name": report_base["mapping_name"],
                        "notes_imported": int(bool(report_base["notes_imported"])),
                    },
                )
                for entry in fresh:
                    Database.insert_entry(conn, entry, batch_id)
        except sqlite3.Error as exc:
            # Rolled back by Database.transaction: nothing partial survives.
            return self._fail_commit(path, report_base, file_hash, file_bytes, exc)

        disposition = self._move(path, self.config.paths.processed_dir)
        return ImportReport(
            **report_base,
            batch_id=batch_id,
            status=BatchStatus.SUCCESS.value,
            inserted_count=len(fresh),
            duplicate_count=duplicate_count,
            file_disposition=disposition,
            message=f"Imported {len(fresh)} new entries.",
        )

    def _handle_duplicate_file(
        self, path: Path, base: dict[str, Any], previous: sqlite3.Row, dry_run: bool
    ) -> ImportReport:
        # The earlier import's entry count is what this file would have
        # contributed, so report it as the duplicate count rather than 0 — a
        # bare "0 inserted, 0 duplicates" reads like an empty file.
        already_stored = previous["inserted_count"]
        message = (
            "This exact file was already imported (matching content hash); "
            f"its {already_stored} entries are already stored and none were added."
        )
        if dry_run:
            return ImportReport(
                **base,
                status="dry_run",
                duplicate_count=already_stored,
                file_disposition="unchanged",
                message="Dry run: " + message,
            )
        batch_id = uuid.uuid4().hex
        with self.db.transaction() as conn:
            Database.insert_batch(
                conn,
                {
                    "id": batch_id,
                    "source": base["source"],
                    "source_type": base["source_type"],
                    "filename": base["filename"],
                    "file_hash": previous["file_hash"],
                    "file_bytes": previous["file_bytes"],
                    "imported_at": _utc_now_iso(),
                    "record_count": 0,
                    "inserted_count": 0,
                    "duplicate_count": already_stored,
                    "rejected_count": 0,
                    "status": BatchStatus.DUPLICATE_FILE.value,
                    "error_message": message,
                    "parser_version": PARSER_VERSION,
                    "mapping_name": base["mapping_name"],
                    "notes_imported": 0,
                },
            )
        disposition = self._move(path, self.config.paths.processed_dir)
        return ImportReport(
            **base,
            batch_id=batch_id,
            status=BatchStatus.DUPLICATE_FILE.value,
            duplicate_count=already_stored,
            file_disposition=disposition,
            message=message,
        )

    def _fail_validation(
        self,
        path: Path,
        base: dict[str, Any],
        file_hash: str,
        file_bytes: int,
        message: str,
        dry_run: bool,
    ) -> ImportReport:
        if dry_run:
            return ImportReport(
                **base,
                status="dry_run",
                file_disposition="unchanged",
                message=f"Dry run: validation failed — {message}",
            )
        batch_id = uuid.uuid4().hex
        self._record_failed_batch(
            batch_id, base, file_hash, file_bytes, BatchStatus.VALIDATION_FAILED, message
        )
        disposition = self._move(path, self.config.paths.failed_dir)
        return ImportReport(
            **base,
            batch_id=batch_id,
            status=BatchStatus.VALIDATION_FAILED.value,
            file_disposition=disposition,
            message=message,
        )

    def _fail_commit(
        self,
        path: Path,
        report_base: dict[str, Any],
        file_hash: str,
        file_bytes: int,
        exc: Exception,
    ) -> ImportReport:
        # The database error text can name table/column internals; log the type
        # for the operator and hand the client a fixed phrase.
        logger.error("Import commit failed: %s", type(exc).__name__)
        message = (
            "The database rejected this import and the transaction was rolled back. "
            "No entries were stored. The source file was left in place so it can be retried."
        )
        batch_id = uuid.uuid4().hex
        recorded = self._record_failed_batch(
            batch_id, report_base, file_hash, file_bytes, BatchStatus.COMMIT_FAILED, message
        )
        return ImportReport(
            **report_base,
            batch_id=batch_id if recorded else None,
            status=BatchStatus.COMMIT_FAILED.value,
            file_disposition="left_in_incoming",
            message=message,
        )

    def _record_failed_batch(
        self,
        batch_id: str,
        base: dict[str, Any],
        file_hash: str,
        file_bytes: int,
        status: BatchStatus,
        message: str,
    ) -> bool:
        try:
            with self.db.transaction() as conn:
                Database.insert_batch(
                    conn,
                    {
                        "id": batch_id,
                        "source": base["source"],
                        "source_type": base["source_type"],
                        "filename": base["filename"],
                        "file_hash": file_hash,
                        "file_bytes": file_bytes,
                        "imported_at": _utc_now_iso(),
                        "record_count": base.get("record_count", 0),
                        "inserted_count": 0,
                        "duplicate_count": 0,
                        "rejected_count": base.get("rejected_count", 0),
                        "status": status.value,
                        "error_message": message,
                        "parser_version": PARSER_VERSION,
                        "mapping_name": base["mapping_name"],
                        "notes_imported": 0,
                    },
                )
            return True
        except sqlite3.Error:
            # The database is the thing that just failed; losing the audit row
            # must not also lose the file.
            logger.error("Could not record a failed import batch.")
            return False

    def _move(self, path: Path, destination_dir: Path) -> str:
        """Move a source file without ever overwriting an existing one.

        Returns a short disposition token, never a host path.
        """
        try:
            destination_dir.mkdir(parents=True, exist_ok=True)
            target = destination_dir / sanitize_filename(path.name)
            if target.exists():
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                target = destination_dir / f"{target.stem}.{stamp}{target.suffix}"
            try:
                os.replace(path, target)
            except OSError:
                # Cross-device move (separate mounts for incoming/processed).
                shutil.move(str(path), str(target))
        except OSError:
            logger.warning("Could not move an imported file out of the incoming directory.")
            return "left_in_incoming"
        return (
            "moved_to_processed"
            if destination_dir == self.config.paths.processed_dir
            else "moved_to_failed"
        )

    # ---- status ------------------------------------------------------------

    def get_import_status(self, limit: int = 20) -> list[dict[str, Any]]:
        """Recent batches, with only sanitized filenames and fixed messages."""
        limit = max(1, min(int(limit), 100))
        conn = self.db.connect()
        try:
            rows = conn.execute(
                """
                SELECT id, source, source_type, filename, imported_at, record_count,
                       inserted_count, duplicate_count, rejected_count, status,
                       error_message, parser_version, mapping_name, notes_imported,
                       substr(file_hash, 1, 12) AS file_hash_prefix
                FROM import_batches
                ORDER BY imported_at DESC, rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "batch_id": row["id"],
                "source": row["source"],
                "source_type": row["source_type"],
                # Basename only — sanitized at write time, never a host path.
                "filename": row["filename"],
                "file_hash_prefix": row["file_hash_prefix"],
                "imported_at": row["imported_at"],
                "record_count": row["record_count"],
                "inserted_count": row["inserted_count"],
                "duplicate_count": row["duplicate_count"],
                "rejected_count": row["rejected_count"],
                "status": row["status"],
                "message": row["error_message"],
                "parser_version": row["parser_version"],
                "mapping_name": row["mapping_name"],
                "notes_imported": bool(row["notes_imported"]),
            }
            for row in rows
        ]

    def list_available_files(self) -> list[str]:
        """Relative names of importable files sitting in the incoming directory."""
        root = self.config.paths.import_root
        if not root.is_dir():
            return []
        names = []
        for child in sorted(root.iterdir()):
            if child.is_file() and child.suffix.lower() in _PARSERS:
                names.append(child.name)
        return names
