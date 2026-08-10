"""Read paths over stored entries.

Two rules shape every query here:

* Column selection is policy-driven. When notes are not permitted the ``note``
  column is not named in the SELECT at all, so there is no code path on which
  note text reaches memory and then has to be remembered to be stripped.
* All filters are bound parameters. No user value is ever formatted into SQL;
  the only interpolation is a run of ``?`` placeholders whose count comes from
  ``len()``.
"""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from ..database import Database
from ..policy import PolicyEngine, PolicyError

__all__ = ["QueryService"]

DEFAULT_LIMIT = 50
HARD_MAX_LIMIT = 500

#: Columns that are always safe to return.
_BASE_COLUMNS = (
    "id",
    "source",
    "occurred_at_utc",
    "utc_offset_minutes",
    "timezone",
    "emotion_label",
    "emotion_family",
    "valence",
    "energy",
    "intensity",
    "tags",
)


def _encode_cursor(occurred_at_utc: str, entry_id: str) -> str:
    payload = json.dumps([occurred_at_utc, entry_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        payload = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        occurred_at_utc, entry_id = json.loads(payload)
        if not isinstance(occurred_at_utc, str) or not isinstance(entry_id, str):
            raise ValueError
    except (ValueError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        raise PolicyError("Invalid pagination cursor.") from None
    return occurred_at_utc, entry_id


def local_time(occurred_at_utc: str, offset_minutes: int) -> datetime:
    """Reconstruct the wall-clock time the entry was recorded at.

    Buckets like "Monday morning" only mean anything in the user's local time,
    so summaries work from this rather than from UTC.
    """
    instant = datetime.fromisoformat(occurred_at_utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(timezone(timedelta(minutes=offset_minutes)))


class QueryService:
    def __init__(self, policy: PolicyEngine, database: Database) -> None:
        self.policy = policy
        self.db = database

    def search_entries(
        self,
        *,
        start: datetime,
        end: datetime,
        emotion_labels: list[str] | None = None,
        tags: list[str] | None = None,
        include_notes: bool = False,
        limit: int | None = None,
        cursor: str | None = None,
        confirm_extended_range: bool = False,
    ) -> dict[str, Any]:
        # Order matters: refuse the whole capability before evaluating filters.
        self.policy.require_raw_entries()
        notes_allowed = self.policy.resolve_include_notes(include_notes)
        start, end = self.policy.validate_range(start, end, confirmed=confirm_extended_range)
        page_size = self.policy.clamp_limit(limit, default=DEFAULT_LIMIT, hard_max=HARD_MAX_LIMIT)

        columns = list(_BASE_COLUMNS) + (["note"] if notes_allowed else [])
        where = ["occurred_at_utc >= ?", "occurred_at_utc < ?"]
        params: list[Any] = [
            start.astimezone(UTC).isoformat(timespec="seconds"),
            end.astimezone(UTC).isoformat(timespec="seconds"),
        ]

        if emotion_labels:
            self.policy.require_emotion_labels()
            cleaned = [
                str(label).strip().casefold() for label in emotion_labels if str(label).strip()
            ]
            if cleaned:
                placeholders = ",".join("?" * len(cleaned))
                where.append(f"LOWER(emotion_label) IN ({placeholders})")
                params.extend(cleaned)

        if tags:
            cleaned_tags = [str(tag).strip().casefold() for tag in tags if str(tag).strip()]
            if cleaned_tags:
                placeholders = ",".join("?" * len(cleaned_tags))
                # Safe: `placeholders` is a run of '?' sized by len();
                # every tag value is bound separately below.
                where.append(
                    f"id IN (SELECT entry_id FROM entry_tags WHERE tag IN ({placeholders}))"  # noqa: S608
                )
                params.extend(cleaned_tags)

        if cursor:
            last_time, last_id = _decode_cursor(cursor)
            where.append("(occurred_at_utc, id) > (?, ?)")
            params.extend([last_time, last_id])

        # Fetch one extra row to learn whether another page exists without
        # running a COUNT over the whole table.
        sql = (
            f"SELECT {', '.join(columns)} FROM mood_entries "  # noqa: S608 - fixed column allowlist
            f"WHERE {' AND '.join(where)} "
            "ORDER BY occurred_at_utc ASC, id ASC LIMIT ?"
        )
        params.append(page_size + 1)

        conn = self.db.connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        has_more = len(rows) > page_size
        rows = rows[:page_size]
        entries = [self._row_to_entry(row, notes_allowed) for row in rows]
        next_cursor = (
            _encode_cursor(rows[-1]["occurred_at_utc"], rows[-1]["id"])
            if has_more and rows
            else None
        )
        return {
            "entries": entries,
            "count": len(entries),
            "has_more": has_more,
            "next_cursor": next_cursor,
            "notes_included": notes_allowed,
            "limit_applied": page_size,
            "range": {"start": start.isoformat(), "end": end.isoformat()},
        }

    @staticmethod
    def _row_to_entry(row: sqlite3.Row, notes_allowed: bool) -> dict[str, Any]:
        entry = {
            "id": row["id"],
            "source": row["source"],
            "occurred_at": local_time(
                row["occurred_at_utc"], row["utc_offset_minutes"]
            ).isoformat(),
            "timezone": row["timezone"],
            "emotion_label": row["emotion_label"],
            "emotion_family": row["emotion_family"],
            "valence": row["valence"],
            "energy": row["energy"],
            "intensity": row["intensity"],
            "tags": json.loads(row["tags"]) if row["tags"] else [],
        }
        if notes_allowed:
            entry["note"] = row["note"]
        return entry

    def fetch_for_aggregation(self, *, start: datetime, end: datetime) -> list[sqlite3.Row]:
        """Rows for summary maths. The note column is never selected here."""
        conn = self.db.connect()
        try:
            return conn.execute(
                """
                SELECT occurred_at_utc, utc_offset_minutes, emotion_label, emotion_family,
                       valence, energy, intensity
                FROM mood_entries
                WHERE occurred_at_utc >= ? AND occurred_at_utc < ?
                ORDER BY occurred_at_utc ASC
                """,
                (
                    start.astimezone(UTC).isoformat(timespec="seconds"),
                    end.astimezone(UTC).isoformat(timespec="seconds"),
                ),
            ).fetchall()
        finally:
            conn.close()
