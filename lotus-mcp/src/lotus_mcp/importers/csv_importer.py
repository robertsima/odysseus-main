"""CSV parsing.

Reads bytes off disk into flat records. Values are carried as text and are
never evaluated, formatted, or interpreted — a cell beginning with ``=`` is
just a string here.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .base import ParsedRecord, ParseError
from .mapping import normalize_key

__all__ = ["parse_csv", "read_header"]

# csv's default field limit is ~128 KB and raises on longer fields. Keep it
# bounded but explicit rather than relying on the module default.
_FIELD_SIZE_LIMIT = 1024 * 1024


def _open(path: Path):
    # utf-8-sig transparently drops a BOM; strict errors turn a mis-encoded
    # file into a clean ParseError instead of mojibake in the database.
    # newline="" is required for quoted multi-line notes to survive.
    return open(path, encoding="utf-8-sig", errors="strict", newline="")


def read_header(path: Path, *, limits: Any) -> list[str]:
    """Return the header row, rejecting duplicate or excessive columns."""
    try:
        with _open(path) as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration:
                raise ParseError("File is empty; a header row is required.") from None
    except UnicodeDecodeError:
        raise ParseError("File is not valid UTF-8 text.") from None
    except csv.Error:
        raise ParseError("File could not be parsed as CSV.") from None

    header = [column.strip() for column in header]
    if not any(header):
        raise ParseError("Header row is blank.")
    if len(header) > limits.max_columns:
        raise ParseError(f"File has more than {limits.max_columns} columns.")

    # DictReader would silently keep only the last of two identical columns,
    # which could swap a note column for an unrelated one. Refuse instead.
    seen: set[str] = set()
    duplicates: set[str] = set()
    for column in header:
        key = normalize_key(column)
        if key in seen:
            duplicates.add(column or "(blank)")
        seen.add(key)
    if duplicates:
        raise ParseError(f"Duplicate column name(s): {', '.join(sorted(duplicates))}.")
    return header


def parse_csv(path: Path, *, limits: Any) -> tuple[list[str], list[ParsedRecord]]:
    """Parse ``path`` into ``(header, records)``."""
    header = read_header(path, limits=limits)
    previous_limit = csv.field_size_limit(_FIELD_SIZE_LIMIT)
    records: list[ParsedRecord] = []
    try:
        with _open(path) as handle:
            reader = csv.reader(handle)
            next(reader, None)  # header, already validated
            for index, row in enumerate(reader, start=1):
                if index > limits.max_records_per_file:
                    raise ParseError(
                        f"File contains more than {limits.max_records_per_file} records."
                    )
                if not any(cell.strip() for cell in row):
                    continue  # blank separator line
                if len(row) > len(header):
                    raise ParseError(f"Row {index} has more fields than the header.")
                values = {
                    column: row[i] if i < len(row) else None for i, column in enumerate(header)
                }
                records.append(ParsedRecord(row=index, values=values))
    except UnicodeDecodeError:
        raise ParseError("File is not valid UTF-8 text.") from None
    except csv.Error:
        # csv.Error messages can quote the offending line; replace it wholesale.
        raise ParseError("File could not be parsed as CSV.") from None
    finally:
        csv.field_size_limit(previous_limit)

    if not records:
        raise ParseError("File contains a header but no data rows.")
    return header, records
