"""The normalized internal mood schema and its validation rules.

Nothing in this module claims to mirror a How We Feel export. It is a generic
adapter schema that configurable mappings project onto (see importers/mapping.py).
"""

from __future__ import annotations

import hashlib
import unicodedata
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .security import hash_note

__all__ = [
    "CONTEXT_MAX_KEYS",
    "MAX_LABEL_CHARS",
    "MAX_TAGS",
    "MAX_TAG_CHARS",
    "RANGES",
    "MoodEntry",
    "SourceType",
    "ValidationProblem",
]

# Supported ranges for the optional affect dimensions. Valence is signed
# (unpleasant .. pleasant); energy and intensity are unsigned magnitudes.
RANGES: dict[str, tuple[float, float]] = {
    "valence": (-1.0, 1.0),
    "energy": (0.0, 1.0),
    "intensity": (0.0, 1.0),
}

MAX_LABEL_CHARS = 120
MAX_TAG_CHARS = 60
MAX_TAGS = 32
CONTEXT_MAX_KEYS = 20
CONTEXT_MAX_VALUE_CHARS = 200

_FIELD_SEP = "\x1f"

Label = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_LABEL_CHARS)
]


class SourceType(StrEnum):
    """Declared provenance of an import file.

    Only ``manual_csv``, ``manual_json`` and ``how_we_feel_export`` are backed
    by a tested parser, and even ``how_we_feel_export`` is only a *label* for a
    user-prepared file — see ``docs`` and the capability matrix in README.md.
    The health-export members are accepted as labels but have no parser and are
    rejected at import time rather than silently mis-parsed.
    """

    HOW_WE_FEEL_EXPORT = "how_we_feel_export"
    APPLE_HEALTH_EXPORT = "apple_health_export"
    HEALTH_CONNECT_EXPORT = "health_connect_export"
    HEALTH_DATA_EXPORT_CSV = "health_data_export_csv"
    MANUAL_CSV = "manual_csv"
    MANUAL_JSON = "manual_json"
    UNKNOWN = "unknown"


#: Source types for which this adapter has an implemented, tested parser.
#: Everything else is refused with an explicit "no verified parser" message so
#: the adapter never pretends to understand a schema it has not seen.
SUPPORTED_SOURCE_TYPES = frozenset(
    {
        SourceType.HOW_WE_FEEL_EXPORT,
        SourceType.MANUAL_CSV,
        SourceType.MANUAL_JSON,
        SourceType.UNKNOWN,
    }
)


class ValidationProblem(BaseModel):
    """A per-record rejection reason, safe to return to a client.

    ``detail`` is a fixed phrase chosen by this codebase — never a cell value,
    never note text, never an exception string from a parser.
    """

    model_config = ConfigDict(frozen=True)

    row: int = Field(description="1-based position of the record within the file.")
    field: str
    detail: str


def normalize_newlines(text: str) -> str:
    """Collapse CRLF/CR to LF.

    A CSV authored on Windows carries CRLF inside quoted multi-line notes. Left
    alone, the same journal entry exported from two machines would hash
    differently and import twice.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _clean_text(value: Any, *, limit: int) -> str | None:
    """NFC-normalize, strip control characters, and bound the length."""
    if value is None:
        return None
    text = normalize_newlines(unicodedata.normalize("NFC", str(value)))
    # Keep newlines and tabs (multiline notes are legitimate); drop the rest of
    # the C0/C1 control range, which only ever arrives from malformed files.
    text = "".join(
        ch for ch in text if ch in "\n\r\t" or not unicodedata.category(ch).startswith("C")
    )
    text = text.strip()
    if not text:
        return None
    return text[:limit]


class MoodEntry(BaseModel):
    """One normalized mood record.

    ``occurred_at`` is always timezone-aware: a naive input is a validation
    error, not something to guess at. ``timezone`` carries the original IANA
    name when the source supplied one, because a fixed UTC offset cannot be
    reconstructed back into a zone.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    source: Label
    source_record_id: str | None = Field(default=None, max_length=200)
    occurred_at: datetime
    timezone: str | None = Field(default=None, max_length=64)
    emotion_label: str | None = Field(default=None, max_length=MAX_LABEL_CHARS)
    emotion_family: str | None = Field(default=None, max_length=MAX_LABEL_CHARS)
    valence: float | None = None
    energy: float | None = None
    intensity: float | None = None
    note: str | None = None
    tags: list[str] = Field(default_factory=list)
    context: dict[str, str] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def _require_awareness(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("occurred_at must carry a timezone offset")
        return value

    @field_validator("valence", "energy", "intensity")
    @classmethod
    def _check_range(cls, value: float | None, info: Any) -> float | None:
        if value is None:
            return None
        low, high = RANGES[info.field_name]
        if not (low <= value <= high):
            raise ValueError(f"{info.field_name} must be between {low} and {high}")
        # Round to avoid float noise leaking into fingerprints.
        return round(float(value), 6)

    @field_validator("emotion_label", "emotion_family")
    @classmethod
    def _clean_label(cls, value: str | None) -> str | None:
        return _clean_text(value, limit=MAX_LABEL_CHARS)

    @field_validator("note")
    @classmethod
    def _clean_note(cls, value: str | None) -> str | None:
        """Normalize line endings only — note text is otherwise kept verbatim."""
        return normalize_newlines(value) if value is not None else None

    @field_validator("tags", mode="before")
    @classmethod
    def _clean_tags(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = value.replace(";", ",").split(",")
        if not isinstance(value, (list, tuple)):
            raise ValueError("tags must be a list or a delimited string")
        seen: list[str] = []
        for item in value:
            tag = _clean_text(item, limit=MAX_TAG_CHARS)
            if tag and tag not in seen:
                seen.append(tag)
        return seen[:MAX_TAGS]

    @field_validator("context", mode="before")
    @classmethod
    def _clean_context(cls, value: Any) -> dict[str, str]:
        """Retain unknown source fields, but only as bounded flat strings.

        Nested structures are stringified rather than stored, so a deeply
        nested JSON object cannot smuggle unbounded data through ``context``.
        """
        if not value:
            return {}
        if not isinstance(value, dict):
            raise ValueError("context must be an object")
        cleaned: dict[str, str] = {}
        for key, raw in list(value.items())[:CONTEXT_MAX_KEYS]:
            safe_key = _clean_text(key, limit=MAX_TAG_CHARS)
            safe_value = _clean_text(raw, limit=CONTEXT_MAX_VALUE_CHARS)
            if safe_key and safe_value is not None:
                cleaned[safe_key] = safe_value
        return cleaned

    @property
    def occurred_at_utc(self) -> datetime:
        return self.occurred_at.astimezone(UTC)

    @property
    def utc_offset_minutes(self) -> int:
        offset = self.occurred_at.utcoffset()
        return int(offset.total_seconds() // 60) if offset else 0

    @property
    def note_hash(self) -> str:
        return hash_note(self.note)

    def fingerprint(self) -> str:
        """Deterministic identity for an entry that carries no source id.

        Derived from the normalized instant, the emotion fields, the affect
        values, the sorted tags, and the *hash* of the note. Raw note text is
        never an input to this digest, so the fingerprint is safe to log, index
        and return.
        """

        def num(value: float | None) -> str:
            return "" if value is None else f"{value:.6f}"

        def text(value: str | None) -> str:
            return "" if value is None else unicodedata.normalize("NFC", value).casefold()

        parts = [
            text(self.source),
            self.occurred_at_utc.isoformat(timespec="seconds"),
            text(self.emotion_label),
            text(self.emotion_family),
            num(self.valence),
            num(self.energy),
            num(self.intensity),
            ",".join(sorted(text(tag) for tag in self.tags)),
            self.note_hash,
        ]
        return hashlib.sha256(_FIELD_SEP.join(parts).encode("utf-8")).hexdigest()

    def redacted(self) -> MoodEntry:
        """A copy with the note removed, for any path that must not carry it."""
        return self.model_copy(update={"note": None})
