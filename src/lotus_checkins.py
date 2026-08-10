"""Owner-isolated daily check-ins backed by Lotus's normalized mood schema."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.runtime_paths import get_app_root

_APP_ROOT = Path(get_app_root())
_LOTUS_SOURCE = _APP_ROOT / "lotus-mcp" / "src"
if str(_LOTUS_SOURCE) not in sys.path:
    sys.path.insert(0, str(_LOTUS_SOURCE))

_database_module = importlib.import_module("lotus_mcp.database")
_models_module = importlib.import_module("lotus_mcp.models")
_query_module = importlib.import_module("lotus_mcp.services.query_service")
BatchStatus = _database_module.BatchStatus
Database = _database_module.Database
MoodEntry = _models_module.MoodEntry
local_time = _query_module.local_time


def _owner_scope(owner: str) -> str:
    return owner if owner else "__single_user__"


def owner_storage_key(owner: str) -> str:
    """Return an opaque stable directory name without exposing usernames."""
    return hashlib.sha256(_owner_scope(owner).encode("utf-8")).hexdigest()[:32]


def _lotus_data_root() -> Path:
    configured = os.environ.get("LOTUS_DATA_DIR", "").strip()
    return Path(configured) if configured else _APP_ROOT / "data" / "lotus" / "data"


class LotusCheckinStore:
    """A physically separate Lotus database for one Odysseus owner."""

    def __init__(self, owner: str) -> None:
        self.owner = _owner_scope(owner)
        self.directory = _lotus_data_root() / "users" / owner_storage_key(owner)
        self.database = Database(self.directory / "mood.db")
        self.database.initialize()
        self._initialize_preferences()

    def _initialize_preferences(self) -> None:
        with self.database.transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lotus_preferences (
                    id                 INTEGER PRIMARY KEY CHECK (id = 1),
                    timezone           TEXT NOT NULL DEFAULT 'UTC',
                    reminder_enabled   INTEGER NOT NULL DEFAULT 0,
                    reminder_times     TEXT NOT NULL DEFAULT '[]',
                    reminder_weekdays  TEXT NOT NULL DEFAULT '[0,1,2,3,4,5,6]',
                    quiet_start        TEXT,
                    quiet_end          TEXT,
                    snooze_minutes     INTEGER NOT NULL DEFAULT 30,
                    updated_at         TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _serialize_row(row, *, include_note: bool = True) -> dict[str, Any]:
        result = {
            "id": row["id"],
            "occurred_at": local_time(
                row["occurred_at_utc"], row["utc_offset_minutes"]
            ).isoformat(),
            "timezone": row["timezone"],
            "emotion_label": row["emotion_label"],
            "emotion_family": row["emotion_family"],
            "valence": row["valence"],
            "energy": row["energy"],
            "intensity": row["intensity"],
            "tags": json.loads(row["tags"] or "[]"),
            "context": json.loads(row["context"] or "{}"),
        }
        if include_note:
            result["note"] = row["note"]
        return result

    def create_checkin(self, values: dict[str, Any]) -> dict[str, Any]:
        entry = MoodEntry(source="daily_checkin", **values)
        batch_id = f"checkin-{entry.id}"
        now = datetime.now(UTC).isoformat(timespec="seconds")
        batch = {
            "id": batch_id,
            "source": "daily_checkin",
            "source_type": "daily_checkin",
            "filename": "daily-checkin",
            "file_hash": hashlib.sha256(batch_id.encode("ascii")).hexdigest(),
            "file_bytes": 0,
            "imported_at": now,
            "record_count": 1,
            "inserted_count": 1,
            "duplicate_count": 0,
            "rejected_count": 0,
            "status": BatchStatus.SUCCESS.value,
            "error_message": None,
            "parser_version": "ui-v1",
            "mapping_name": "daily_checkin",
            "notes_imported": int(bool(entry.note)),
        }
        with self.database.transaction() as conn:
            Database.insert_batch(conn, batch)
            Database.insert_entry(conn, entry, batch_id)
        return self.get_checkin(entry.id)

    def get_checkin(self, entry_id: str) -> dict[str, Any]:
        conn = self.database.connect()
        try:
            row = conn.execute(
                "SELECT * FROM mood_entries WHERE id = ? AND source = 'daily_checkin'",
                (entry_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise KeyError(entry_id)
        return self._serialize_row(row)

    def list_checkins(
        self, *, limit: int = 100, before: str | None = None
    ) -> list[dict[str, Any]]:
        where = ["source = 'daily_checkin'"]
        params: list[Any] = []
        if before:
            where.append("occurred_at_utc < ?")
            params.append(before)
        params.append(max(1, min(limit, 365)))
        conn = self.database.connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM mood_entries WHERE {' AND '.join(where)} "
                "ORDER BY occurred_at_utc DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        finally:
            conn.close()
        return [self._serialize_row(row) for row in rows]

    def delete_checkin(self, entry_id: str) -> bool:
        with self.database.transaction() as conn:
            row = conn.execute(
                "SELECT batch_id FROM mood_entries WHERE id = ? AND source = 'daily_checkin'",
                (entry_id,),
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM import_batches WHERE id = ?", (row["batch_id"],))
            return True

    def overview(self) -> dict[str, Any]:
        entries = self.list_checkins(limit=365)
        conn = self.database.connect()
        try:
            total_checkins = conn.execute(
                "SELECT COUNT(*) FROM mood_entries WHERE source = 'daily_checkin'"
            ).fetchone()[0]
        finally:
            conn.close()
        now = datetime.now(UTC)
        last_30 = []
        for entry in entries:
            moment = datetime.fromisoformat(entry["occurred_at"])
            if (now - moment.astimezone(UTC)).days < 30:
                last_30.append(entry)
        averages = {}
        for field in ("valence", "energy", "intensity"):
            values = [
                float(entry[field]) for entry in last_30 if entry[field] is not None
            ]
            averages[field] = round(sum(values) / len(values), 3) if values else None
        return {
            "total_checkins": total_checkins,
            "last_30_days": len(last_30),
            "last_checkin": entries[0] if entries else None,
            "averages_30_days": averages,
        }

    def get_preferences(self) -> dict[str, Any]:
        conn = self.database.connect()
        try:
            row = conn.execute(
                "SELECT * FROM lotus_preferences WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return {
                "timezone": "UTC",
                "reminder_enabled": False,
                "reminder_times": [],
                "reminder_weekdays": list(range(7)),
                "quiet_start": None,
                "quiet_end": None,
                "snooze_minutes": 30,
            }
        return {
            "timezone": row["timezone"],
            "reminder_enabled": bool(row["reminder_enabled"]),
            "reminder_times": json.loads(row["reminder_times"]),
            "reminder_weekdays": json.loads(row["reminder_weekdays"]),
            "quiet_start": row["quiet_start"],
            "quiet_end": row["quiet_end"],
            "snooze_minutes": row["snooze_minutes"],
        }

    def save_preferences(self, values: dict[str, Any]) -> dict[str, Any]:
        payload = {
            **self.get_preferences(),
            **values,
        }
        with self.database.transaction() as conn:
            conn.execute(
                """
                INSERT INTO lotus_preferences (
                    id, timezone, reminder_enabled, reminder_times, reminder_weekdays,
                    quiet_start, quiet_end, snooze_minutes, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    timezone = excluded.timezone,
                    reminder_enabled = excluded.reminder_enabled,
                    reminder_times = excluded.reminder_times,
                    reminder_weekdays = excluded.reminder_weekdays,
                    quiet_start = excluded.quiet_start,
                    quiet_end = excluded.quiet_end,
                    snooze_minutes = excluded.snooze_minutes,
                    updated_at = excluded.updated_at
                """,
                (
                    payload["timezone"],
                    int(payload["reminder_enabled"]),
                    json.dumps(payload["reminder_times"]),
                    json.dumps(payload["reminder_weekdays"]),
                    payload["quiet_start"],
                    payload["quiet_end"],
                    payload["snooze_minutes"],
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )
        return self.get_preferences()
