"""Shared parser types.

A parser's only job is to turn bytes on disk into a stream of flat
``{column: text}`` records. It performs no interpretation of meaning — that
belongs to the mapping layer — and it never raises an exception whose message
could contain file content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

__all__ = ["ParseError", "ParsedRecord", "Parser", "RecordSource"]


class RecordSource(StrEnum):
    CSV = "csv"
    JSON = "json"


class ParseError(Exception):
    """A file-level parse failure with a message safe to show a client.

    Parser call sites construct these from fixed phrases plus structural
    details (line numbers, column counts). Never from cell values.
    """


@dataclass(slots=True)
class ParsedRecord:
    """One raw record, straight off the file."""

    #: 1-based index within the file, used for error reporting only.
    row: int
    values: dict[str, Any] = field(default_factory=dict)


class Parser(Protocol):
    """Extension point: a new source format only has to satisfy this.

    Implementations are wired in ``services/import_service.py`` by extension
    and declared source type; adding one does not touch the policy, dedup, or
    MCP layers.
    """

    def __call__(self, path: Any, *, limits: Any) -> list[ParsedRecord]: ...
