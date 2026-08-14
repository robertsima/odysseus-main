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


def owner_has_lotus_data(owner: str) -> bool:
    """Whether this owner already has a Lotus database on disk.

    Constructing a ``LotusCheckinStore`` creates one as a side effect, so the
    per-tick reminder scanner has to ask this first — otherwise it would
    fabricate an empty mood database for every account on the system.
    """
    return (_lotus_data_root() / "users" / owner_storage_key(owner) / "mood.db").is_file()


#: Every preference column with its SQL type and default. The table is
#: migrated additively from this map (see ``_initialize_preferences``), so a
#: database written by an older build upgrades in place on first load instead
#: of needing a versioned migration step.
_PREFERENCE_COLUMNS: dict[str, tuple[str, Any]] = {
    "timezone": ("TEXT NOT NULL DEFAULT 'UTC'", "UTC"),
    "reminder_enabled": ("INTEGER NOT NULL DEFAULT 0", False),
    "reminder_times": ("TEXT NOT NULL DEFAULT '[]'", []),
    "reminder_weekdays": ("TEXT NOT NULL DEFAULT '[0,1,2,3,4,5,6]'", [0, 1, 2, 3, 4, 5, 6]),
    "quiet_start": ("TEXT", None),
    "quiet_end": ("TEXT", None),
    "snooze_minutes": ("INTEGER NOT NULL DEFAULT 30", 30),
    # Milestone 3 — delivery customization.
    "channel": ("TEXT NOT NULL DEFAULT 'inherit'", "inherit"),
    "min_hours_between": ("INTEGER NOT NULL DEFAULT 0", 0),
    "skip_if_checked_in": ("INTEGER NOT NULL DEFAULT 1", True),
    "message_style": ("TEXT NOT NULL DEFAULT 'plain'", "plain"),
    "paused_until": ("TEXT", None),
    "insights_enabled": ("INTEGER NOT NULL DEFAULT 0", False),
    "insights_frequency": ("TEXT NOT NULL DEFAULT 'weekly'", "weekly"),
    "insights_weekday": ("INTEGER NOT NULL DEFAULT 6", 6),
    "insights_time": ("TEXT NOT NULL DEFAULT '09:00'", "09:00"),
    # Owner-controlled model access policy. These defaults preserve the
    # original behavior: loopback and LAN/Tailscale allowed, public APIs off.
    "access_local": ("INTEGER NOT NULL DEFAULT 1", True),
    "access_lan": ("INTEGER NOT NULL DEFAULT 1", True),
    "access_api": ("INTEGER NOT NULL DEFAULT 0", False),
}

_JSON_PREFERENCES = ("reminder_times", "reminder_weekdays")
_BOOL_PREFERENCES = (
    "reminder_enabled",
    "skip_if_checked_in",
    "insights_enabled",
    "access_local",
    "access_lan",
    "access_api",
)

PREFERENCE_DEFAULTS: dict[str, Any] = {
    name: (list(default) if isinstance(default, list) else default)
    for name, (_sql, default) in _PREFERENCE_COLUMNS.items()
}


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
                    updated_at         TEXT NOT NULL
                )
                """
            )
            # Additive migration: a database created by an earlier build has
            # only the original seven columns, and one created before this
            # milestone has none of the delivery columns. Add whatever is
            # missing rather than versioning the table, so an existing install
            # with real check-ins upgrades silently on first load.
            existing = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(lotus_preferences)").fetchall()
            }
            for name, (definition, _default) in _PREFERENCE_COLUMNS.items():
                if name not in existing:
                    conn.execute(
                        f"ALTER TABLE lotus_preferences ADD COLUMN {name} {definition}"  # noqa: S608
                    )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lotus_notifications (
                    id           TEXT PRIMARY KEY,
                    kind         TEXT NOT NULL,
                    title        TEXT NOT NULL,
                    body         TEXT NOT NULL,
                    channel      TEXT NOT NULL,
                    dedupe_key   TEXT NOT NULL,
                    created_at   TEXT NOT NULL,
                    delivered    INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lotus_notifications_created "
                "ON lotus_notifications(created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lotus_notifications_dedupe "
                "ON lotus_notifications(dedupe_key, delivered, created_at)"
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

    def last_checkin_at(self) -> str | None:
        """UTC timestamp of the most recent check-in, or None."""
        conn = self.database.connect()
        try:
            row = conn.execute(
                "SELECT MAX(occurred_at_utc) FROM mood_entries WHERE source = 'daily_checkin'"
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row and row[0] else None

    def get_preferences(self) -> dict[str, Any]:
        conn = self.database.connect()
        try:
            row = conn.execute(
                "SELECT * FROM lotus_preferences WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        result = dict(PREFERENCE_DEFAULTS)
        if row is None:
            return result
        keys = row.keys()
        for name in _PREFERENCE_COLUMNS:
            if name not in keys:
                continue
            value = row[name]
            if value is None and name not in ("quiet_start", "quiet_end", "paused_until"):
                continue
            if name in _JSON_PREFERENCES:
                result[name] = json.loads(value)
            elif name in _BOOL_PREFERENCES:
                result[name] = bool(value)
            else:
                result[name] = value
        return result

    def save_preferences(self, values: dict[str, Any]) -> dict[str, Any]:
        # Merge over the current row so a caller that knows only some fields
        # (an older UI build, or the snooze endpoint) cannot blank the rest.
        payload = {**self.get_preferences(), **values}
        names = list(_PREFERENCE_COLUMNS)
        stored = []
        for name in names:
            value = payload.get(name, PREFERENCE_DEFAULTS[name])
            if name in _JSON_PREFERENCES:
                stored.append(json.dumps(list(value or [])))
            elif name in _BOOL_PREFERENCES:
                stored.append(int(bool(value)))
            else:
                stored.append(value)
        columns = ", ".join(names)
        placeholders = ", ".join("?" * len(names))
        updates = ", ".join(f"{name} = excluded.{name}" for name in names)
        with self.database.transaction() as conn:
            conn.execute(
                f"INSERT INTO lotus_preferences (id, {columns}, updated_at) "  # noqa: S608
                f"VALUES (1, {placeholders}, ?) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}, updated_at = excluded.updated_at",
                (*stored, datetime.now(UTC).isoformat(timespec="seconds")),
            )
        return self.get_preferences()

    # ---- notification history ---------------------------------------------

    def record_notification(
        self,
        *,
        kind: str,
        title: str,
        body: str,
        channel: str,
        dedupe_key: str,
        delivered: bool,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """Persist one delivery attempt inside the owner's own Lotus database.

        This lives here rather than in a ``data/<username>.json`` file on
        purpose: Lotus never writes an owner's name to disk (the directory is
        a hash), and the same rows double as the in-app history the UI shows.
        """
        moment = created_at or datetime.now(UTC).isoformat(timespec="seconds")
        entry_id = hashlib.sha256(f"{dedupe_key}|{moment}".encode("utf-8")).hexdigest()[:32]
        with self.database.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO lotus_notifications "
                "(id, kind, title, body, channel, dedupe_key, created_at, delivered) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (entry_id, kind, title, body, channel, dedupe_key, moment, int(bool(delivered))),
            )
        return {
            "id": entry_id,
            "kind": kind,
            "title": title,
            "body": body,
            "channel": channel,
            "dedupe_key": dedupe_key,
            "created_at": moment,
            "delivered": bool(delivered),
        }

    def list_notifications(self, *, limit: int = 50) -> list[dict[str, Any]]:
        conn = self.database.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM lotus_notifications ORDER BY created_at DESC, id DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "id": row["id"],
                "kind": row["kind"],
                "title": row["title"],
                "body": row["body"],
                "channel": row["channel"],
                "created_at": row["created_at"],
                "delivered": bool(row["delivered"]),
            }
            for row in rows
        ]

    def last_notification_at(self, *, kind: str | None = None, delivered_only: bool = True) -> str | None:
        where = []
        params: list[Any] = []
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if delivered_only:
            where.append("delivered = 1")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        conn = self.database.connect()
        try:
            row = conn.execute(
                f"SELECT MAX(created_at) FROM lotus_notifications {clause}",  # noqa: S608
                params,
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row and row[0] else None

    def notification_delivered_since(self, dedupe_key: str, since: str) -> bool:
        """Whether a scheduled occurrence was already delivered.

        Check-in keys intentionally stay stable across days so browser
        notifications replace an older card for the same configured time.
        ``since`` scopes the durable dedupe check to the current occurrence.
        """
        conn = self.database.connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM lotus_notifications "
                "WHERE dedupe_key = ? AND delivered = 1 AND created_at >= ? LIMIT 1",
                (dedupe_key, since),
            ).fetchone()
        finally:
            conn.close()
        return row is not None
