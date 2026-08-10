"""SQLite storage: deterministic schema, explicit transactions, batch rollback.

Storage is plain SQLite. It is **not** encrypted by this application — see
PRIVACY.md and the ``encryption_status`` reported by the health command. Do not
read anything more into a Docker volume mount than "a directory on the host".
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .models import MoodEntry
from .security import install_note_safe_logging

__all__ = ["SCHEMA_VERSION", "BatchStatus", "Database"]

logger = install_note_safe_logging(logging.getLogger(__name__))

SCHEMA_VERSION = 1


class BatchStatus(StrEnum):
    """Terminal states for an import attempt.

    A batch is never left "in progress": either the whole insert committed, or
    the transaction rolled back and the batch is recorded as failed.
    """

    SUCCESS = "success"
    DUPLICATE_FILE = "duplicate_file"
    VALIDATION_FAILED = "validation_failed"
    COMMIT_FAILED = "commit_failed"
    ROLLED_BACK = "rolled_back"


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS import_batches (
        id              TEXT PRIMARY KEY,
        source          TEXT NOT NULL,
        source_type     TEXT NOT NULL,
        filename        TEXT NOT NULL,
        file_hash       TEXT NOT NULL,
        file_bytes      INTEGER NOT NULL DEFAULT 0,
        imported_at     TEXT NOT NULL,
        record_count    INTEGER NOT NULL DEFAULT 0,
        inserted_count  INTEGER NOT NULL DEFAULT 0,
        duplicate_count INTEGER NOT NULL DEFAULT 0,
        rejected_count  INTEGER NOT NULL DEFAULT 0,
        status          TEXT NOT NULL,
        error_message   TEXT,
        parser_version  TEXT NOT NULL,
        mapping_name    TEXT NOT NULL,
        notes_imported  INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mood_entries (
        id                 TEXT PRIMARY KEY,
        batch_id           TEXT NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
        source             TEXT NOT NULL,
        source_record_id   TEXT,
        occurred_at_utc    TEXT NOT NULL,
        utc_offset_minutes INTEGER NOT NULL DEFAULT 0,
        timezone           TEXT,
        emotion_label      TEXT,
        emotion_family     TEXT,
        valence            REAL,
        energy             REAL,
        intensity          REAL,
        note               TEXT,
        note_hash          TEXT NOT NULL DEFAULT '',
        tags               TEXT NOT NULL DEFAULT '[]',
        context            TEXT NOT NULL DEFAULT '{}',
        fingerprint        TEXT NOT NULL,
        created_at         TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS entry_tags (
        entry_id TEXT NOT NULL REFERENCES mood_entries(id) ON DELETE CASCADE,
        tag      TEXT NOT NULL,
        PRIMARY KEY (entry_id, tag)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS consent_policies (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        recorded_at TEXT NOT NULL,
        policy_json TEXT NOT NULL,
        policy_hash TEXT NOT NULL UNIQUE
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_entries_fingerprint ON mood_entries(fingerprint)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ix_entries_source_record
        ON mood_entries(source, source_record_id) WHERE source_record_id IS NOT NULL
    """,
    "CREATE INDEX IF NOT EXISTS ix_entries_occurred ON mood_entries(occurred_at_utc)",
    "CREATE INDEX IF NOT EXISTS ix_entries_label ON mood_entries(emotion_label)",
    "CREATE INDEX IF NOT EXISTS ix_entries_batch ON mood_entries(batch_id)",
    "CREATE INDEX IF NOT EXISTS ix_tags_tag ON entry_tags(tag)",
    "CREATE INDEX IF NOT EXISTS ix_batches_hash ON import_batches(file_hash)",
    "CREATE INDEX IF NOT EXISTS ix_batches_time ON import_batches(imported_at)",
)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    """Thin, explicit wrapper over a single SQLite file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # ---- connection management --------------------------------------------

    def connect(self) -> sqlite3.Connection:
        """Open a configured connection.

        ``isolation_level=None`` disables sqlite3's implicit transaction
        handling so every write path states its own BEGIN/COMMIT/ROLLBACK.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            # Some network filesystems reject WAL. Journaling still works in
            # rollback mode, so this is a performance note, not a failure.
            logger.warning("WAL journal mode unavailable; continuing with the default journal.")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a unit of work atomically.

        BEGIN IMMEDIATE takes the write lock up front so a concurrent importer
        fails fast instead of halfway through.
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    # ---- schema ------------------------------------------------------------

    def initialize(self) -> None:
        """Create the schema if absent. Safe to call on every start."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def schema_version(self) -> int:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            return int(row["value"]) if row else 0
        finally:
            conn.close()

    # ---- consent snapshot --------------------------------------------------

    def record_policy(self, policy: dict[str, Any]) -> None:
        """Persist the active consent policy so its history is auditable."""
        payload = json.dumps(policy, sort_keys=True)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO consent_policies(recorded_at, policy_json, policy_hash) "
                "VALUES (?, ?, ?)",
                (_utc_now_iso(), payload, digest),
            )

    # ---- writes ------------------------------------------------------------

    @staticmethod
    def insert_batch(conn: sqlite3.Connection, batch: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO import_batches (
                id, source, source_type, filename, file_hash, file_bytes, imported_at,
                record_count, inserted_count, duplicate_count, rejected_count,
                status, error_message, parser_version, mapping_name, notes_imported
            ) VALUES (
                :id, :source, :source_type, :filename, :file_hash, :file_bytes, :imported_at,
                :record_count, :inserted_count, :duplicate_count, :rejected_count,
                :status, :error_message, :parser_version, :mapping_name, :notes_imported
            )
            """,
            batch,
        )

    @staticmethod
    def insert_entry(conn: sqlite3.Connection, entry: MoodEntry, batch_id: str) -> None:
        """Insert one entry and its tag rows inside the caller's transaction."""
        conn.execute(
            """
            INSERT INTO mood_entries (
                id, batch_id, source, source_record_id, occurred_at_utc, utc_offset_minutes,
                timezone, emotion_label, emotion_family, valence, energy, intensity,
                note, note_hash, tags, context, fingerprint, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.id,
                batch_id,
                entry.source,
                entry.source_record_id,
                entry.occurred_at_utc.isoformat(timespec="seconds"),
                entry.utc_offset_minutes,
                entry.timezone,
                entry.emotion_label,
                entry.emotion_family,
                entry.valence,
                entry.energy,
                entry.intensity,
                entry.note,
                entry.note_hash,
                json.dumps(entry.tags, ensure_ascii=False),
                json.dumps(entry.context, ensure_ascii=False),
                entry.fingerprint(),
                _utc_now_iso(),
            ),
        )
        for tag in entry.tags:
            conn.execute(
                "INSERT OR IGNORE INTO entry_tags(entry_id, tag) VALUES (?, ?)",
                (entry.id, tag.casefold()),
            )

    # ---- reads used by dedup ----------------------------------------------

    @staticmethod
    def existing_fingerprints(conn: sqlite3.Connection, fingerprints: list[str]) -> set[str]:
        """Which of ``fingerprints`` are already stored.

        Chunked to stay under SQLite's variable limit on very large imports.
        """
        found: set[str] = set()
        for start in range(0, len(fingerprints), 500):
            chunk = fingerprints[start : start + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT fingerprint FROM mood_entries WHERE fingerprint IN ({placeholders})",  # noqa: S608
                chunk,
            ).fetchall()
            found.update(row["fingerprint"] for row in rows)
        return found

    @staticmethod
    def existing_source_ids(conn: sqlite3.Connection, source: str, ids: list[str]) -> set[str]:
        found: set[str] = set()
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                "SELECT source_record_id FROM mood_entries "  # noqa: S608
                f"WHERE source = ? AND source_record_id IN ({placeholders})",
                [source, *chunk],
            ).fetchall()
            found.update(row["source_record_id"] for row in rows)
        return found

    def find_batch_by_file_hash(self, file_hash: str) -> sqlite3.Row | None:
        conn = self.connect()
        try:
            return conn.execute(
                "SELECT * FROM import_batches WHERE file_hash = ? AND status = ? "
                "ORDER BY imported_at DESC LIMIT 1",
                (file_hash, BatchStatus.SUCCESS.value),
            ).fetchone()
        finally:
            conn.close()

    # ---- administrative operations ----------------------------------------

    def rollback_batch(self, batch_id: str) -> int:
        """Delete every entry from one batch and mark the batch rolled back."""
        with self.transaction() as conn:
            row = conn.execute("SELECT id FROM import_batches WHERE id = ?", (batch_id,)).fetchone()
            if row is None:
                raise KeyError(batch_id)
            cursor = conn.execute("DELETE FROM mood_entries WHERE batch_id = ?", (batch_id,))
            deleted = cursor.rowcount or 0
            conn.execute(
                "UPDATE import_batches SET status = ?, inserted_count = 0 WHERE id = ?",
                (BatchStatus.ROLLED_BACK.value, batch_id),
            )
            return deleted

    def redact_all_notes(self) -> int:
        """Blank every stored note, keeping the hash so dedup still works."""
        with self.transaction() as conn:
            cursor = conn.execute("UPDATE mood_entries SET note = NULL WHERE note IS NOT NULL")
            conn.execute("UPDATE import_batches SET notes_imported = 0")
            return cursor.rowcount or 0

    def counts(self) -> dict[str, int]:
        """Row counts only — never content. Used by health and CLI output."""
        conn = self.connect()
        try:
            entries = conn.execute("SELECT COUNT(*) AS n FROM mood_entries").fetchone()["n"]
            batches = conn.execute("SELECT COUNT(*) AS n FROM import_batches").fetchone()["n"]
            notes = conn.execute(
                "SELECT COUNT(*) AS n FROM mood_entries WHERE note IS NOT NULL"
            ).fetchone()["n"]
            return {"entries": entries, "batches": batches, "entries_with_notes": notes}
        finally:
            conn.close()
