"""JSON parsing.

Depth is checked on the raw text *before* ``json.loads`` runs, so a
deeply-nested document is refused without building the object graph.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import ParsedRecord, ParseError

__all__ = ["parse_json", "scan_max_depth"]

#: Keys a top-level object may use to hold the record list.
_RECORD_KEYS = ("entries", "records", "data", "items", "moods")


def scan_max_depth(text: str) -> int:
    """Maximum bracket nesting in ``text``, ignoring brackets inside strings."""
    depth = 0
    deepest = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif char in "]}":
            depth -= 1
    return deepest


def _flatten(value: Any) -> Any:
    """Keep scalars and scalar lists; collapse nested objects to compact JSON.

    The mapping layer only consumes flat values, and ``context`` is bounded
    text — so a nested object becomes an inert string rather than a structure
    that could grow without limit.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        if all(v is None or isinstance(v, (str, int, float, bool)) for v in value):
            return value
        return json.dumps(value, ensure_ascii=False)[:2000]
    return json.dumps(value, ensure_ascii=False)[:2000]


def parse_json(path: Path, *, limits: Any) -> tuple[list[str], list[ParsedRecord]]:
    """Parse ``path`` into ``(columns, records)``."""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        raise ParseError("File is not valid UTF-8 text.") from None

    if not text.strip():
        raise ParseError("File is empty.")

    depth = scan_max_depth(text)
    if depth > limits.max_json_depth:
        raise ParseError(f"JSON nesting exceeds the maximum depth of {limits.max_json_depth}.")

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        # Position is structural and safe; the message may quote content.
        raise ParseError(
            f"File is not valid JSON (line {exc.lineno}, column {exc.colno})."
        ) from None

    if isinstance(document, dict):
        for key in _RECORD_KEYS:
            if isinstance(document.get(key), list):
                rows = document[key]
                break
        else:
            raise ParseError(
                "JSON object must contain a list under one of: " + ", ".join(_RECORD_KEYS) + "."
            )
    elif isinstance(document, list):
        rows = document
    else:
        raise ParseError("JSON must be a list of records or an object containing one.")

    if len(rows) > limits.max_records_per_file:
        raise ParseError(f"File contains more than {limits.max_records_per_file} records.")

    columns: list[str] = []
    records: list[ParsedRecord] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ParseError(f"Record {index} is not a JSON object.")
        if len(row) > limits.max_columns:
            raise ParseError(f"Record {index} has more than {limits.max_columns} fields.")
        values = {str(key): _flatten(value) for key, value in row.items()}
        for key in values:
            if key not in columns:
                columns.append(key)
        records.append(ParsedRecord(row=index, values=values))

    if not records:
        raise ParseError("File contains no records.")
    return columns, records
