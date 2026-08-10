"""Configurable, inspectable field mapping.

No official How We Feel export schema has been verified, so this adapter never
guesses at column meaning. An operator declares aliases; a column that matches
no alias is either dropped or retained verbatim in ``context``. Sensitive
fields (notes above all) are only populated by an exact alias match.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime, timedelta
from datetime import date as date_cls
from datetime import time as time_cls
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models import RANGES, MoodEntry, ValidationProblem
from .base import ParsedRecord

__all__ = ["FieldMapping", "RowNormalizer", "Scale", "default_mappings"]

#: Fields the mapping can populate. ``date``/``time`` are inputs that combine
#: into ``occurred_at`` when a source splits them across two columns.
MAPPABLE_FIELDS = (
    "occurred_at",
    "date",
    "time",
    "timezone",
    "source_record_id",
    "emotion_label",
    "emotion_family",
    "valence",
    "energy",
    "intensity",
    "note",
    "tags",
)

_TRUE_ISH = {"1", "true", "yes", "y"}

# Accepted without a configured format. Anything else must be declared in
# ``timestamp_formats`` — the adapter does not go fishing for a format.
_FALLBACK_TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M",
    "%m/%d/%Y %H:%M",
    "%d/%m/%Y %H:%M",
)
_FALLBACK_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y")
_FALLBACK_TIME_FORMATS = ("%H:%M:%S", "%H:%M", "%I:%M %p", "%I:%M:%S %p")

_KEY_NOISE = re.compile(r"[\s\-./]+")


def normalize_key(key: Any) -> str:
    """Fold a column name to a comparable token (``"Date / Time" -> "date_time"``)."""
    text = unicodedata.normalize("NFKC", str(key)).strip().casefold()
    return _KEY_NOISE.sub("_", text).strip("_")


class Scale(BaseModel):
    """Linear rescaling of a source range onto this adapter's canonical range.

    Declared per field, e.g. a 1-5 "pleasantness" column becomes valence in
    [-1, 1]. Without a scale the value is taken as already canonical.
    """

    model_config = ConfigDict(extra="forbid")

    in_min: float
    in_max: float

    @field_validator("in_max")
    @classmethod
    def _distinct(cls, value: float, info: Any) -> float:
        if info.data.get("in_min") == value:
            raise ValueError("scale in_min and in_max must differ")
        return value

    def apply(self, value: float, field_name: str) -> float:
        out_min, out_max = RANGES[field_name]
        ratio = (value - self.in_min) / (self.in_max - self.in_min)
        scaled = out_min + ratio * (out_max - out_min)
        # Clamp: a source value slightly outside its declared range is a data
        # quirk, not a reason to drop an otherwise good record.
        return min(max(scaled, out_min), out_max)


class FieldMapping(BaseModel):
    """A named, fully explicit mapping from source columns to the internal schema."""

    model_config = ConfigDict(extra="forbid")

    description: str = ""
    #: Value written to ``MoodEntry.source``.
    source_label: str = "manual_csv"
    aliases: dict[str, list[str]] = Field(default_factory=dict)
    scales: dict[str, Scale] = Field(default_factory=dict)
    timestamp_formats: list[str] = Field(default_factory=list)
    date_formats: list[str] = Field(default_factory=list)
    time_formats: list[str] = Field(default_factory=list)
    #: An IANA zone applied to naive timestamps. ``None`` means a naive
    #: timestamp is rejected — the adapter will not invent a zone.
    assume_timezone: str | None = None
    #: Keep unmatched columns in ``MoodEntry.context`` (bounded, flat strings).
    retain_unknown_fields: bool = False

    @field_validator("aliases")
    @classmethod
    def _known_fields(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        unknown = sorted(set(value) - set(MAPPABLE_FIELDS))
        if unknown:
            raise ValueError(f"Unknown mapped field(s): {', '.join(unknown)}")
        return value

    @field_validator("scales")
    @classmethod
    def _scalable_fields(cls, value: dict[str, Scale]) -> dict[str, Scale]:
        unknown = sorted(set(value) - set(RANGES))
        if unknown:
            raise ValueError(f"Scales are only supported for {', '.join(sorted(RANGES))}")
        return value

    @field_validator("assume_timezone")
    @classmethod
    def _valid_zone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown IANA timezone: {value}") from exc
        return value

    def alias_index(self) -> dict[str, str]:
        """``normalized alias -> field name``, for inspection and resolution."""
        index: dict[str, str] = {}
        for field_name, aliases in self.aliases.items():
            for alias in aliases:
                index.setdefault(normalize_key(alias), field_name)
        return index

    def describe(self) -> dict[str, Any]:
        """A client-safe description of what this mapping will read."""
        return {
            "description": self.description,
            "source_label": self.source_label,
            "aliases": {k: list(v) for k, v in sorted(self.aliases.items())},
            "scales": {k: {"in_min": v.in_min, "in_max": v.in_max} for k, v in self.scales.items()},
            "assume_timezone": self.assume_timezone,
            "retain_unknown_fields": self.retain_unknown_fields,
        }


def default_mappings() -> dict[str, FieldMapping]:
    """Built-in mappings.

    ``how_we_feel_generic`` is an *adapter convention* for a user-prepared
    file. It is explicitly not a verified How We Feel export schema.
    """
    generic_aliases = {
        "occurred_at": ["timestamp", "datetime", "date_time", "occurred_at", "recorded_at", "when"],
        "date": ["date", "day"],
        "time": ["time", "time_of_day"],
        "timezone": ["timezone", "tz", "time_zone"],
        "source_record_id": ["id", "record_id", "entry_id", "uuid"],
        "emotion_label": ["emotion", "feeling", "mood", "emotion_label", "label"],
        "emotion_family": ["emotion_family", "family", "category", "quadrant"],
        "valence": ["valence", "pleasantness", "pleasant"],
        "energy": ["energy", "arousal"],
        "intensity": ["intensity", "strength", "magnitude"],
        "note": ["note", "notes", "journal", "comment", "entry_note"],
        "tags": ["tags", "tag", "context", "labels"],
    }
    return {
        "generic": FieldMapping(
            description="Generic mood CSV/JSON columns. Adapter convention, not a vendor schema.",
            source_label="manual_csv",
            aliases=generic_aliases,
            retain_unknown_fields=False,
        ),
        "how_we_feel_generic": FieldMapping(
            description=(
                "Convention for a user-prepared file labelled how_we_feel_export. "
                "NOT a verified How We Feel export schema — no official export has been confirmed."
            ),
            source_label="how_we_feel_export",
            aliases=generic_aliases,
            retain_unknown_fields=False,
        ),
    }


def _parse_offset_suffix(text: str) -> str:
    """Normalize a trailing ``Z`` so ``fromisoformat`` accepts it on 3.11."""
    return text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text


def _apply_zone(naive: datetime, zone_name: str) -> datetime:
    """Attach ``zone_name`` to a naive wall time, refusing ambiguous instants.

    A local time inside a DST fall-back hour maps to two real instants, and one
    inside a spring-forward gap maps to none. Both are refused rather than
    resolved by coin flip.
    """
    zone = ZoneInfo(zone_name)
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError("ambiguous local time (daylight-saving fall-back)")
    if first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != naive:
        raise ValueError("nonexistent local time (daylight-saving spring-forward)")
    return first


class RowNormalizer:
    """Turns raw parsed records into validated :class:`MoodEntry` objects."""

    def __init__(self, mapping: FieldMapping, limits: Any, *, import_notes: bool) -> None:
        self.mapping = mapping
        self.limits = limits
        #: Notes are only read off disk when the *server* allows it. When
        #: False the note column is never even copied into memory.
        self.import_notes = import_notes
        self._alias_index = mapping.alias_index()

    # ---- column resolution -------------------------------------------------

    def resolve_columns(self, columns: list[str]) -> dict[str, str]:
        """Map field name -> the actual column that will feed it.

        First match wins, so a file with both ``mood`` and ``emotion`` uses
        whichever alias the operator listed first.
        """
        resolved: dict[str, str] = {}
        for column in columns:
            field_name = self._alias_index.get(normalize_key(column))
            if field_name and field_name not in resolved:
                resolved[field_name] = column
        return resolved

    def unmapped_columns(self, columns: list[str]) -> list[str]:
        return [c for c in columns if normalize_key(c) not in self._alias_index]

    # ---- value coercion ----------------------------------------------------

    def _text(self, raw: Any) -> str | None:
        if raw is None:
            return None
        if isinstance(raw, (list, tuple)):
            raw = ", ".join(str(part) for part in raw)
        text = str(raw).strip()
        return text[: self.limits.max_field_chars] if text else None

    def _number(self, raw: Any, field_name: str) -> float | None:
        text = self._text(raw)
        if text is None:
            return None
        try:
            value = float(text)
        except ValueError:
            raise ValueError("not a number") from None
        scale = self.mapping.scales.get(field_name)
        if scale is not None:
            return scale.apply(value, field_name)
        return value

    def _timestamp(
        self, values: dict[str, Any], resolved: dict[str, str]
    ) -> tuple[datetime, str | None]:
        """Build a timezone-aware instant, or raise with a safe reason."""
        zone_name = self._text(values.get(resolved.get("timezone", ""), None))
        if zone_name:
            try:
                ZoneInfo(zone_name)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("unknown timezone name") from None

        raw = self._text(values.get(resolved.get("occurred_at", ""), None))
        parsed: datetime | None = None
        if raw:
            parsed = self._parse_datetime(raw)
        else:
            parsed = self._parse_date_time_pair(values, resolved)

        if parsed is None:
            raise ValueError("missing timestamp")

        if parsed.tzinfo is not None:
            # An explicit offset in the data wins; the zone name (if any) is
            # kept alongside it because an offset alone cannot be turned back
            # into a zone.
            return parsed, zone_name
        # Naive: only a declared zone may resolve it. Never guessed.
        declared = zone_name or self.mapping.assume_timezone
        if not declared:
            raise ValueError(
                "timestamp has no timezone and the mapping declares no assume_timezone"
            )
        return _apply_zone(parsed, declared), declared

    def _parse_datetime(self, raw: str) -> datetime:
        try:
            return datetime.fromisoformat(_parse_offset_suffix(raw))
        except ValueError:
            pass
        for fmt in (*self.mapping.timestamp_formats, *_FALLBACK_TIMESTAMP_FORMATS):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        raise ValueError("unrecognized timestamp format")

    def _parse_date_time_pair(
        self, values: dict[str, Any], resolved: dict[str, str]
    ) -> datetime | None:
        raw_date = self._text(values.get(resolved.get("date", ""), None))
        if not raw_date:
            return None
        parsed_date: date_cls | None = None
        for fmt in (*self.mapping.date_formats, *_FALLBACK_DATE_FORMATS):
            try:
                parsed_date = datetime.strptime(raw_date, fmt).date()
                break
            except ValueError:
                continue
        if parsed_date is None:
            try:
                parsed_date = date_cls.fromisoformat(raw_date)
            except ValueError:
                raise ValueError("unrecognized date format") from None

        raw_time = self._text(values.get(resolved.get("time", ""), None))
        if not raw_time:
            # A date with no time is ambiguous to the minute; midnight would be
            # a guess that silently shifts entries across day boundaries.
            raise ValueError("date supplied without a time")
        for fmt in (*self.mapping.time_formats, *_FALLBACK_TIME_FORMATS):
            try:
                parsed_time = datetime.strptime(raw_time, fmt).time()
                break
            except ValueError:
                continue
        else:
            try:
                parsed_time = time_cls.fromisoformat(raw_time)
            except ValueError:
                raise ValueError("unrecognized time format") from None
        return datetime.combine(parsed_date, parsed_time)

    def _note(self, values: dict[str, Any], resolved: dict[str, str]) -> str | None:
        if not self.import_notes or "note" not in resolved:
            return None
        raw = values.get(resolved["note"])
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        if len(text) > self.limits.max_note_chars:
            if self.limits.oversize_note_policy == "truncate":
                return text[: self.limits.max_note_chars]
            raise ValueError(f"note exceeds max_note_chars ({self.limits.max_note_chars})")
        return text

    # ---- entry construction ------------------------------------------------

    def normalize(
        self, record: ParsedRecord, resolved: dict[str, str], unmapped: list[str]
    ) -> tuple[MoodEntry | None, ValidationProblem | None]:
        """Return either a validated entry or a safe rejection reason."""
        values = record.values
        try:
            occurred_at, zone_name = self._timestamp(values, resolved)
        except ValueError as exc:
            return None, ValidationProblem(row=record.row, field="occurred_at", detail=str(exc))

        numbers: dict[str, float | None] = {}
        for field_name in ("valence", "energy", "intensity"):
            column = resolved.get(field_name)
            if column is None:
                numbers[field_name] = None
                continue
            try:
                numbers[field_name] = self._number(values.get(column), field_name)
            except ValueError as exc:
                return None, ValidationProblem(row=record.row, field=field_name, detail=str(exc))

        try:
            note = self._note(values, resolved)
        except ValueError as exc:
            # The reason mentions the limit, never the note itself.
            return None, ValidationProblem(row=record.row, field="note", detail=str(exc))

        context: dict[str, Any] = {}
        if self.mapping.retain_unknown_fields:
            context = {
                name: values.get(name) for name in unmapped if values.get(name) not in (None, "")
            }

        try:
            entry = MoodEntry(
                source=self.mapping.source_label,
                source_record_id=self._text(values.get(resolved.get("source_record_id", ""), None)),
                occurred_at=occurred_at,
                timezone=zone_name,
                emotion_label=self._text(values.get(resolved.get("emotion_label", ""), None)),
                emotion_family=self._text(values.get(resolved.get("emotion_family", ""), None)),
                note=note,
                tags=values.get(resolved.get("tags", ""), None),
                context=context,
                **numbers,
            )
        except Exception as exc:  # pydantic ValidationError and friends
            return None, ValidationProblem(
                row=record.row, field="record", detail=_safe_validation_detail(exc)
            )
        return entry, None


def _safe_validation_detail(exc: Exception) -> str:
    """Summarize a validation failure without echoing any field value.

    Pydantic error messages can embed the offending input, which for a note
    would mean journal text in an error response. Only field names and error
    types are kept.
    """
    errors = getattr(exc, "errors", None)
    if callable(errors):
        parts = []
        for err in errors():
            location = ".".join(str(p) for p in err.get("loc", ())) or "record"
            parts.append(f"{location}: {err.get('type', 'invalid')}")
        if parts:
            return "; ".join(sorted(set(parts))[:5])
    return "record failed schema validation"


def is_true(value: Any) -> bool:
    return str(value).strip().casefold() in _TRUE_ISH


def utc_now() -> datetime:
    return datetime.now(UTC)


def days_between(start: datetime, end: datetime) -> float:
    return (end - start) / timedelta(days=1)
