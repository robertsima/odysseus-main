"""Obsidian-aware parsing and chunking for Markdown knowledge bases.

The generic indexer in ``rag_vector`` treats a ``.md`` file as a wall of prose:
it strips nothing, understands nothing, and splits on sentence boundaries. For
an Obsidian-style vault that throws away most of what the file actually
encodes:

* **YAML frontmatter** carries the tags, aliases and dates that say what a note
  *is*. Left in the body it is retrieval noise (``---``, ``tags:``, bare list
  dashes) embedded into the first chunk of every note.
* **Headings** are the note's outline. A chunk lifted from under
  ``## Deployment > ### NAS`` reads as orphaned prose once the heading is gone,
  and the words "deployment" and "NAS" — the ones a query would actually use —
  are not in the chunk at all.
* **``[[wikilinks]]``** are the vault's own graph. A note that says "see
  [[Sarah]]" is asserting a relationship that no embedding will recover.
* **Dates** are what make a knowledge base answerable over time. Without them
  a note superseded eighteen months ago competes on equal terms with the one
  that replaced it, and the model has no way to tell which was true when.

This module extracts all four and hands them back as plain data. It has no
ChromaDB or embedding dependency so it can be exercised directly in tests.

Metadata encoding note: ChromaDB metadata values must be scalars, so list-like
fields (tags, aliases, links) are stored as pipe-delimited strings with leading
and trailing pipes — ``"|project|ai|"``. The sentinel pipes make an exact
membership test a plain substring check (``"|ai|" in value``) that cannot match
a longer neighbouring tag. Use :func:`encode_list` / :func:`decode_list` rather
than assembling those strings by hand.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

MARKDOWN_EXTENSIONS = {".md", ".markdown"}

# Frontmatter keys read for each derived field, in priority order.
_TAG_KEYS = ("tags", "tag", "keywords")
_ALIAS_KEYS = ("aliases", "alias")
# "When was this last asserted" beats "when was it first written": a note's
# usefulness for conflict resolution is its most recent revision, not its
# birthday. created/date are the fallbacks for notes that never record an
# update.
_DATE_KEYS = ("updated", "modified", "last_updated", "date", "created")

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# [[target]], [[target|label]], [[target#heading]], [[folder/target]]
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+)(?:#[^\[\]|]*)?(?:\|[^\[\]]*)?\]\]")
# Inline #tag. Requires a non-digit somewhere so "#1" and "#2026" (issue refs,
# years) are not swept in as tags. Allows nesting ("#project/odysseus").
_INLINE_TAG_RE = re.compile(r"(?<![\w&])#([A-Za-z0-9_\-/]*[A-Za-z_][A-Za-z0-9_\-/]*)")

_LIST_SEP = "|"

# Date shapes that appear in note *filenames*. Ordered most-specific first.
_FILENAME_DATE_PATTERNS = (
    re.compile(r"(?P<y>\d{4})[-_.](?P<m>\d{1,2})[-_.](?P<d>\d{1,2})"),   # 2026-08-14
    re.compile(r"(?P<a>\d{1,2})[-_.](?P<b>\d{1,2})[-_.](?P<y>\d{4})"),   # 08-14-2026 / 14-08-2026
    re.compile(r"(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})(?!\d)"),           # 20260814
)


def _date_order() -> str:
    """``day`` or ``month`` — how to read an ambiguous ``08-08-2026`` filename.

    Only matters when both leading components are <= 12; otherwise the shape is
    self-disambiguating. Defaults to day-first. A vault written in US order and
    left on the default still gets the right *year* out of every filename, which
    is what recency ranking is actually sensitive to, so this is a precision
    knob rather than a correctness one.
    """
    raw = (os.environ.get("ODYSSEUS_VAULT_DATE_ORDER") or "day").strip().lower()
    return "month" if raw.startswith("m") else "day"


# ---------------------------------------------------------------------------
# List encoding for Chroma scalar metadata
# ---------------------------------------------------------------------------


def encode_list(values: Sequence[str]) -> str:
    """Pack a list into a sentinel-delimited scalar. Empty list -> ``""``."""
    cleaned = []
    seen = set()
    for value in values:
        text = str(value or "").strip().lower()
        if not text or _LIST_SEP in text:
            # A value containing the separator would corrupt the encoding.
            text = text.replace(_LIST_SEP, "/")
            if not text:
                continue
        if text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    if not cleaned:
        return ""
    return _LIST_SEP + _LIST_SEP.join(cleaned) + _LIST_SEP


def decode_list(value: Any) -> List[str]:
    """Unpack :func:`encode_list`. Tolerates ``None`` and legacy plain lists."""
    if isinstance(value, (list, tuple)):
        return [str(v).strip().lower() for v in value if str(v).strip()]
    if not isinstance(value, str) or not value:
        return []
    return [part for part in value.split(_LIST_SEP) if part]


def list_contains(value: Any, needle: str) -> bool:
    """Exact membership test against an :func:`encode_list` scalar."""
    needle = (needle or "").strip().lower()
    if not needle:
        return False
    if isinstance(value, str) and value:
        return f"{_LIST_SEP}{needle}{_LIST_SEP}" in value
    return needle in decode_list(value)


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------


def split_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Return ``(frontmatter_dict, body)``.

    A file without frontmatter, or with frontmatter that does not parse, yields
    ``({}, text)`` — an unreadable header must never cost us the note's prose.

    A leading BOM is stripped first. Editors on Windows write one routinely,
    and it is invisible in every tool that displays the file — but it sits
    before the opening ``---``, so the frontmatter pattern (anchored with
    ``\\A``) does not match and the note silently loses every tag, alias and
    date it declared. The same byte hides a first-line ``# Heading`` from the
    heading pattern. Both failures are undetectable by reading the note.
    """
    if not text:
        return {}, ""
    text = text.lstrip("﻿")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    body = text[match.end():]
    raw = match.group(1)
    try:
        import yaml

        parsed = yaml.safe_load(raw)
    except Exception as e:
        logger.debug("frontmatter parse failed (%s); treating note as body-only", e)
        return {}, body
    if not isinstance(parsed, dict):
        return {}, body
    return parsed, body


def _as_string_list(value: Any) -> List[str]:
    """Coerce a frontmatter value into a list of strings.

    Obsidian accepts every one of ``tags: a, b``, ``tags: [a, b]`` and a YAML
    block list, so all three have to work.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,\n]", value)
        return [p.strip().lstrip("#") for p in parts if p.strip()]
    if isinstance(value, (list, tuple, set)):
        out: List[str] = []
        for item in value:
            out.extend(_as_string_list(item))
        return out
    return [str(value).strip()]


def _first_key(mapping: Dict[str, Any], keys: Sequence[str]) -> Any:
    lowered = {str(k).strip().lower(): v for k, v in mapping.items()}
    for key in keys:
        if key in lowered and lowered[key] not in (None, ""):
            return lowered[key]
    return None


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def _epoch(year: int, month: int, day: int) -> Optional[float]:
    try:
        return datetime(year, month, day, tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def parse_date_value(value: Any) -> Optional[float]:
    """Best-effort epoch seconds from a frontmatter date value."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        stamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return stamp.timestamp()
    # PyYAML turns a bare `2026-08-14` into datetime.date, which has no
    # timestamp() of its own.
    if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
        return _epoch(int(value.year), int(value.month), int(value.day))
    if isinstance(value, (int, float)):
        # Plausible epoch seconds (1990..2100). Anything else is a year or an
        # ordinal we should not guess at.
        if 631152000 <= float(value) <= 4102444800:
            return float(value)
        return None
    text = str(value).strip()
    if not text:
        return None
    iso = text.replace("/", "-")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(iso[:len(fmt) + 2].strip(), fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        pass
    return date_from_filename(text)


def date_from_filename(name: str) -> Optional[float]:
    """Epoch seconds for a date embedded in a note name, else ``None``."""
    if not name:
        return None
    stem = Path(name).stem or name
    for pattern in _FILENAME_DATE_PATTERNS:
        match = pattern.search(stem)
        if not match:
            continue
        groups = match.groupdict()
        year = int(groups["y"])
        if "m" in groups and groups.get("m") is not None:
            month, day = int(groups["m"]), int(groups["d"])
        else:
            a, b = int(groups["a"]), int(groups["b"])
            if a > 12:
                month, day = b, a
            elif b > 12:
                month, day = a, b
            elif _date_order() == "month":
                month, day = a, b
            else:
                month, day = b, a
        stamp = _epoch(year, month, day)
        if stamp is not None:
            return stamp
    return None


def format_date(epoch: Optional[float]) -> str:
    """``YYYY-MM-DD`` for display, or ``""`` when unknown."""
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# Wikilinks, tags, note keys
# ---------------------------------------------------------------------------


def note_key(name: str) -> str:
    """Canonical form Obsidian resolves ``[[links]]`` against: the bare stem."""
    if not name:
        return ""
    tail = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    stem = Path(tail).stem if Path(tail).suffix.lower() in MARKDOWN_EXTENSIONS else tail
    return stem.strip().lower()


def extract_wikilinks(text: str) -> List[str]:
    """Resolved ``note_key`` for every ``[[wikilink]]`` in *text*."""
    if not text:
        return []
    out: List[str] = []
    seen = set()
    for raw in _WIKILINK_RE.findall(text):
        key = note_key(raw)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def extract_inline_tags(text: str) -> List[str]:
    """``#tags`` written in the body, ignoring fenced code and headings."""
    if not text:
        return []
    out: List[str] = []
    seen = set()
    for line in _iter_prose_lines(text):
        if _HEADING_RE.match(line):
            continue
        for raw in _INLINE_TAG_RE.findall(line):
            tag = raw.strip().strip("/").lower()
            if tag and tag not in seen:
                seen.add(tag)
                out.append(tag)
    return out


def _iter_prose_lines(text: str):
    """Lines outside fenced code blocks.

    Everything that reads Markdown structure has to agree on where code starts
    and ends, or a ``#!/usr/bin/env`` inside a shell block becomes a heading and
    a ``# TODO`` comment becomes a tag.
    """
    in_fence = False
    fence_marker = ""
    for line in text.splitlines():
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_marker = True, marker
                continue
            if marker == fence_marker:
                in_fence, fence_marker = False, ""
                continue
        if in_fence:
            continue
        yield line


# ---------------------------------------------------------------------------
# Parsed document
# ---------------------------------------------------------------------------


@dataclass
class MarkdownDoc:
    filename: str
    title: str
    body: str
    frontmatter: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)
    links: List[str] = field(default_factory=list)
    doc_date: Optional[float] = None
    doc_date_source: str = ""

    @property
    def key(self) -> str:
        return note_key(self.filename)


def parse_markdown(text: str, filename: str, mtime: Optional[float] = None) -> MarkdownDoc:
    """Parse one note into frontmatter, body, tags, aliases, links and a date.

    ``mtime`` is the last-resort date. It is recorded as
    ``doc_date_source="mtime"`` so ranking can discount it: a filesystem
    timestamp moves when a note is touched, re-synced or restored from backup,
    none of which mean its content became newly true.
    """
    frontmatter, body = split_frontmatter(text or "")

    tags = _as_string_list(_first_key(frontmatter, _TAG_KEYS))
    tags = [t.strip().lstrip("#").strip("/").lower() for t in tags if t.strip()]
    for tag in extract_inline_tags(body):
        if tag not in tags:
            tags.append(tag)

    aliases = [a.strip().lower() for a in _as_string_list(_first_key(frontmatter, _ALIAS_KEYS)) if a.strip()]
    links = extract_wikilinks(body)

    title = ""
    for key in ("title", "name"):
        raw_title = _first_key(frontmatter, (key,))
        if isinstance(raw_title, str) and raw_title.strip():
            title = raw_title.strip()
            break
    if not title:
        for line in _iter_prose_lines(body):
            heading = _HEADING_RE.match(line)
            if heading and heading.group(2).strip():
                title = heading.group(2).strip()
                break
    if not title:
        title = Path(filename).stem

    doc_date = parse_date_value(_first_key(frontmatter, _DATE_KEYS))
    source = "frontmatter" if doc_date else ""
    if doc_date is None:
        doc_date = date_from_filename(filename)
        source = "filename" if doc_date else ""
    if doc_date is None and mtime:
        doc_date, source = float(mtime), "mtime"

    return MarkdownDoc(
        filename=filename,
        title=title,
        body=body,
        frontmatter=frontmatter if isinstance(frontmatter, dict) else {},
        tags=tags,
        aliases=aliases,
        links=links,
        doc_date=doc_date,
        doc_date_source=source,
    )


# ---------------------------------------------------------------------------
# Heading-aware chunking
# ---------------------------------------------------------------------------


@dataclass
class MarkdownChunk:
    text: str
    heading_path: str
    links: List[str] = field(default_factory=list)


def _sections(body: str) -> List[Tuple[str, str]]:
    """Split a body into ``(heading_path, section_text)`` in document order.

    The heading lines stay in the section text. They are part of what the
    section says, and keeping them means the embedded chunk still reads as
    Markdown rather than as decapitated prose.
    """
    sections: List[Tuple[str, str]] = []
    stack: List[str] = []
    current: List[str] = []
    current_path = ""

    def flush() -> None:
        text = "\n".join(current).strip()
        if text:
            sections.append((current_path, text))

    in_fence = False
    fence_marker = ""
    for line in (body or "").splitlines():
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_marker = True, marker
            elif marker == fence_marker:
                in_fence, fence_marker = False, ""
            current.append(line)
            continue

        heading = None if in_fence else _HEADING_RE.match(line)
        if heading:
            flush()
            current = []
            level = len(heading.group(1))
            title = heading.group(2).strip()
            del stack[level - 1:]
            while len(stack) < level - 1:
                stack.append("")
            stack.append(title)
            current_path = " > ".join(p for p in stack if p)
        current.append(line)

    flush()
    return sections


def chunk_markdown(
    doc: MarkdownDoc,
    split_fn: Callable[[str], List[str]],
    chunk_size: int = 1000,
) -> List[MarkdownChunk]:
    """Chunk a parsed note along heading boundaries.

    Sections are packed together until adding the next one would exceed
    ``chunk_size``, so a note of one-line headings does not explode into dozens
    of near-empty chunks. A single section larger than ``chunk_size`` is handed
    to ``split_fn`` — the caller's existing sentence-aware splitter — so the
    within-section behaviour stays exactly what it was before this module
    existed.

    ``heading_path`` on a packed chunk is the path of the section it *starts*
    at; the remaining headings are still present verbatim in the text.
    """
    sections = _sections(doc.body)
    if not sections:
        stripped = (doc.body or "").strip()
        if not stripped:
            return []
        return [
            MarkdownChunk(text=piece, heading_path="", links=extract_wikilinks(piece))
            for piece in split_fn(stripped)
        ]

    chunks: List[MarkdownChunk] = []
    buffer: List[str] = []
    buffer_len = 0
    buffer_path = ""

    def flush_buffer() -> None:
        nonlocal buffer, buffer_len, buffer_path
        if not buffer:
            return
        text = "\n\n".join(buffer).strip()
        if text:
            chunks.append(
                MarkdownChunk(text=text, heading_path=buffer_path, links=extract_wikilinks(text))
            )
        buffer, buffer_len, buffer_path = [], 0, ""

    for path, text in sections:
        if len(text) > chunk_size:
            flush_buffer()
            for piece in split_fn(text):
                if piece.strip():
                    chunks.append(
                        MarkdownChunk(text=piece, heading_path=path, links=extract_wikilinks(piece))
                    )
            continue

        if buffer and buffer_len + len(text) + 2 > chunk_size:
            flush_buffer()
        if not buffer:
            buffer_path = path
        buffer.append(text)
        buffer_len += len(text) + 2

    flush_buffer()
    return chunks


# ---------------------------------------------------------------------------
# Chunk provenance header
# ---------------------------------------------------------------------------

_HEADER_FIELDS = ("Source", "Section", "Tags", "Aliases", "Updated")
_HEADER_LINE_RE = re.compile(rf"^({'|'.join(_HEADER_FIELDS)}): ")


def build_chunk_header(
    filename: str,
    heading_path: str = "",
    tags: Optional[Sequence[str]] = None,
    aliases: Optional[Sequence[str]] = None,
    doc_date: Optional[float] = None,
) -> str:
    """Provenance block prepended to a chunk *before it is embedded*.

    Everything here exists because retrieval scores against chunk **text**:
    the embedding is computed from it and the keyword half of the hybrid score
    tokenises it. A tag, a section name or a date held only in metadata is
    invisible to both. Putting them in the text is what makes "the deployment
    section of the NAS note" and "#project notes from August" answerable at
    all. Costs roughly fifteen tokens a chunk.

    Emitted lines are stripped again by :func:`strip_chunk_header` before the
    text is shown to the model, because the caller re-renders the same facts
    from metadata in a single compact line per snippet.
    """
    lines = []
    stem = Path(filename).stem
    if stem and stem != filename:
        lines.append(f"Source: {filename} {stem}")
    else:
        lines.append(f"Source: {filename}")
    if heading_path:
        lines.append(f"Section: {heading_path}")
    if tags:
        lines.append("Tags: " + " ".join(f"#{t}" for t in tags))
    if aliases:
        lines.append("Aliases: " + ", ".join(aliases))
    stamp = format_date(doc_date)
    if stamp:
        lines.append(f"Updated: {stamp}")
    return "\n".join(lines)


def describe_chunk(meta: Any) -> str:
    """One-line provenance label for a retrieved chunk, built from metadata.

    The date is the point of this. Without it the model sees two contradictory
    notes as one contradictory corpus and picks arbitrarily; with it, "the 2026
    note supersedes the 2024 one" is a conclusion it can reach and show its
    working for. Section and tags cost a handful of tokens and let it say
    *where* in the vault an answer came from.

    Rendered from metadata rather than from the chunk's embedded header so it
    stays correct for chunks indexed before that header existed.
    """
    if not isinstance(meta, dict):
        return "unknown"
    parts = [str(meta.get("filename") or meta.get("source") or "unknown")]
    stamp = format_date(meta.get("doc_date"))
    if stamp:
        # A journal named for its day says when it was written about, not when
        # it was last revised — so don't claim the note was "updated" then.
        label = "dated" if meta.get("doc_date_source") == "filename" else "updated"
        parts.append(f"{label} {stamp}")
    heading = meta.get("heading_path")
    if heading:
        parts.append(f"section: {heading}")
    tags = decode_list(meta.get("tags"))
    if tags:
        parts.append("tags: " + " ".join(f"#{t}" for t in tags[:6]))
    return " · ".join(parts)


def strip_chunk_header(text: str) -> str:
    """Remove a leading :func:`build_chunk_header` block, if present.

    Only consecutive leading lines matching a known field are dropped, so a
    note whose own first line happens to read ``Source: ...`` loses at most that
    line — and a chunk written before headers existed is returned untouched.
    """
    if not text:
        return ""
    lines = text.splitlines()
    index = 0
    while index < len(lines) and _HEADER_LINE_RE.match(lines[index]):
        index += 1
    if not index:
        return text
    while index < len(lines) and not lines[index].strip():
        index += 1
    return "\n".join(lines[index:])
