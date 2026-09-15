"""The note <-> Markdown codec.

Notes used to be SQLite rows with no privacy label at all (see ``Note`` in
``core/database.py``). The vault already has folder-scoped sensitivity and is
plain Markdown, editable in Obsidian or any other editor. Making a note *be* a
Markdown file — rather than mirroring it into one — means it inherits the
vault's privacy policy for free and stops being a second, ungated place
personal content lives.

This module only converts between a :class:`NoteRecord` and Markdown text (or
decides a safe filename for one). It knows nothing about SQLite, settings, or
the filesystem beyond a couple of pure path-safety helpers — that keeps it
testable without a database and reusable by both the migration and, later, the
note routes.

Design notes on the encoding, because the field mapping is not obvious:

* ``title`` is written to frontmatter *verbatim*, never sanitized. The
  filename derived from it is a sanitized, truncated, human-friendly label —
  it is NOT the note's identity and does not need to preserve every
  character. Round-trip fidelity for exotic titles (emoji, ``../..``,
  350 characters) comes entirely from the frontmatter copy.
* ``content`` lives in the body verbatim, because the point of this
  migration is that a human can open the file in Obsidian (or any editor)
  and read/edit the prose directly. An earlier version of this codec framed
  the body as ``content + <length-prefixed sections>`` so a section marker
  embedded in the user's own text could never be misparsed — but a length
  prefix cannot survive the very editing this format exists to allow: any
  external edit that changes the body's length either truncates content or
  bleeds other text into it, silently. This version parses *structurally*
  instead, so there is nothing to keep in sync with the body's length and no
  failure mode when a human edits the file.
* ``items`` are the trailing run of contiguous task-list lines
  (``- [ ] ...`` / ``- [x] ...``) at the *end* of the body. Everything before
  that run is ``content``. This is deliberately how a human would expect a
  checklist note to work: adding ``- [ ] pick up parcel`` at the end of the
  file in Obsidian makes it a new item, not a parsing error. Whether the body
  is even scanned for a trailing task run at all is controlled by
  ``x-note-type`` (see below) — a plain note's content is never re-parsed as
  items, even if it happens to end with lines that look like a task list.
* ``x-note-type: checklist`` is written to frontmatter whenever
  ``items is not None`` (including the empty-list case) and omitted for a
  plain note. The original spec wanted ``note_type`` inferred purely from
  whether the body ends in task lines, but that makes an *empty* checklist
  indistinguishable from a plain note with no trailing task lines — there is
  no text left to infer from. An explicit frontmatter key is the only honest
  fix, and Obsidian ignores unknown frontmatter keys, so it costs nothing.
* ``image_url`` is a plain frontmatter scalar (key ``image``), not a link
  embedded in the body. It follows the same None-vs-"" convention already
  used for ``owner``/``due``/``color`` elsewhere in this file: the key is
  omitted entirely for ``None`` and written as an empty string for ``""``.
  Putting the image in the body instead (as a Markdown image link) was
  considered, but there is no reliable way to tell "the note's image" apart
  from an inline image a human later pastes into the content, especially
  after external edits reorder or duplicate links — so the field stays a
  first-class piece of metadata instead of being smuggled into prose it
  would then be ambiguous with.
* ``x-content-empty`` is a small, deliberately narrow flag: the body alone
  cannot distinguish ``content=None`` from ``content=""`` (a plain note with
  no body and a checklist/plain note with an explicitly empty body both
  produce zero characters of body text). This flag is written only when
  ``content == ""`` and is consulted only when the parsed body text is
  itself empty — any real text a human types anywhere in the body makes the
  flag irrelevant. It is metadata about a degenerate case, not a mechanism
  the body's length has to stay in sync with, so it does not reintroduce the
  fragility ``x-content-len`` had.
* ``archived`` is not written to frontmatter either. It is expressed as
  *which directory the file lives in*, so this module exposes
  :func:`resolve_note_directory` for callers that place the file, but
  ``note_to_markdown``/``markdown_to_note`` never touch it — a caller that
  reads a file back sets ``NoteRecord.archived`` from the path it read it
  from.
"""
from __future__ import annotations

import json
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

from src.vault_markdown import split_frontmatter

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
# Deliberately a plain dataclass, not the SQLAlchemy ``Note`` model: this
# module has to work without a database (tests, and eventually a route that
# reads the vault directly), and a dataclass is a stable, explicit contract
# for whatever talks to the ORM on either side of it.


@dataclass
class NoteItem:
    """One checklist row. ``extra`` catches any keys beyond text/done that a
    future client adds, so a codec written today cannot silently drop them."""
    text: str = ""
    done: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NoteRecord:
    """Mirrors every column of ``core.database.Note`` except ``note_type``
    (inferred from ``items``) and the DB-only ``id`` autoincrement concerns."""
    id: str
    owner: Optional[str] = None
    title: str = ""
    content: Optional[str] = None
    items: Optional[List[NoteItem]] = None  # None = plain note; list (maybe []) = checklist
    color: Optional[str] = None
    label: Optional[str] = None
    pinned: bool = False
    archived: bool = False
    due_date: Optional[str] = None
    source: str = "user"
    session_id: Optional[str] = None
    sort_order: int = 0
    image_url: Optional[str] = None
    repeat: str = "none"
    ai_classification: Optional[str] = None
    ai_content_hash: Optional[str] = None
    agent_session_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # Preserve frontmatter owned by the user or other vault tools (notably
    # ``sensitivity`` and Obsidian metadata) when Odysseus updates a note.
    extra_frontmatter: Dict[str, Any] = field(default_factory=dict)

    @property
    def note_type(self) -> str:
        return "checklist" if self.items is not None else "note"


def note_record_from_orm(note: Any) -> NoteRecord:
    """Build a :class:`NoteRecord` from a ``core.database.Note`` row (or
    anything duck-typing it) without importing that module — avoids coupling
    this codec to the ORM/DB stack it is meant to let notes escape.
    """
    items = None
    raw_items = getattr(note, "items", None)
    if raw_items:
        try:
            parsed = json.loads(raw_items)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            items = [_item_from_dict(d) for d in parsed]
    elif getattr(note, "note_type", "note") == "checklist":
        # A checklist with zero items still has to round-trip as a checklist,
        # not silently become a plain note.
        items = []

    return NoteRecord(
        id=str(getattr(note, "id")),
        owner=getattr(note, "owner", None),
        title=getattr(note, "title", "") or "",
        content=getattr(note, "content", None),
        items=items,
        color=getattr(note, "color", None),
        label=getattr(note, "label", None),
        pinned=bool(getattr(note, "pinned", False)),
        archived=bool(getattr(note, "archived", False)),
        due_date=getattr(note, "due_date", None),
        source=getattr(note, "source", None) or "user",
        session_id=getattr(note, "session_id", None),
        sort_order=int(getattr(note, "sort_order", 0) or 0),
        image_url=getattr(note, "image_url", None),
        repeat=getattr(note, "repeat", None) or "none",
        ai_classification=getattr(note, "ai_classification", None),
        ai_content_hash=getattr(note, "ai_content_hash", None),
        agent_session_id=getattr(note, "agent_session_id", None),
        created_at=getattr(note, "created_at", None),
        updated_at=getattr(note, "updated_at", None),
    )


def _item_from_dict(d: Dict[str, Any]) -> NoteItem:
    if not isinstance(d, dict):
        return NoteItem(text=str(d), done=False)
    extra = {k: v for k, v in d.items() if k not in ("text", "done")}
    return NoteItem(text=str(d.get("text", "") or ""), done=bool(d.get("done", False)), extra=extra)


# ---------------------------------------------------------------------------
# Body encoding: content + native task list + image link
# ---------------------------------------------------------------------------

_ITEMS_MARKER = "<!-- odysseus-note:items -->"
_IMAGE_MARKER = "<!-- odysseus-note:image -->"
_TASK_LINE_RE = re.compile(r"^-\s\[([ xX])\]\s?(.*)$")
_ITEM_EXTRA_RE = re.compile(r"\s*<!--\s*(\{.*\})\s*-->\s*$")
_IMAGE_LINE_RE = re.compile(r"^!\[\]\((.*)\)\s*$")


def _escape_item_text(text: str) -> str:
    """Fold a checklist item onto one Markdown task-list line.

    Order matters: backslashes are doubled *before* newlines are turned into
    the two visible characters ``\\n``, so a literal backslash the user typed
    can never be misread as the start of our own escape sequence on decode.
    """
    text = text or ""
    return text.replace("\\", "\\\\").replace("\r\n", "\n").replace("\n", "\\n")


def _unescape_item_text(text: str) -> str:
    out: List[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _encode_items_block(items: Sequence[NoteItem]) -> str:
    lines = [_ITEMS_MARKER]
    for item in items:
        box = "x" if item.done else " "
        line = f"- [{box}] {_escape_item_text(item.text)}"
        if item.extra:
            line += f" <!-- {json.dumps(item.extra, ensure_ascii=False, sort_keys=True)} -->"
        lines.append(line)
    return "\n".join(lines)


def _encode_image_block(image_url: str) -> str:
    return f"{_IMAGE_MARKER}\n![]({image_url})"


def _extract_items_block(remainder: str) -> Optional[List[NoteItem]]:
    start = remainder.find(_ITEMS_MARKER)
    if start == -1:
        return None
    end = remainder.find(_IMAGE_MARKER, start)
    section = remainder[start:end] if end != -1 else remainder[start:]
    items: List[NoteItem] = []
    for line in section.splitlines()[1:]:  # skip the marker line itself
        match = _TASK_LINE_RE.match(line)
        if not match:
            continue
        box, text = match.group(1), match.group(2)
        extra: Dict[str, Any] = {}
        extra_match = _ITEM_EXTRA_RE.search(text)
        if extra_match:
            text = text[: extra_match.start()]
            try:
                parsed = json.loads(extra_match.group(1))
                if isinstance(parsed, dict):
                    extra = parsed
            except json.JSONDecodeError:
                pass
        items.append(NoteItem(text=_unescape_item_text(text), done=box.lower() == "x", extra=extra))
    return items


def _extract_image_block(remainder: str) -> Optional[str]:
    start = remainder.find(_IMAGE_MARKER)
    if start == -1:
        return None
    section = remainder[start:]
    for line in section.splitlines()[1:]:
        if not line.strip():
            continue
        match = _IMAGE_LINE_RE.match(line.strip())
        return match.group(1) if match else None
    return None


def _build_body(content: Optional[str], items: Optional[List[NoteItem]], image_url: Optional[str]) -> str:
    segments: List[str] = []
    if content is not None:
        segments.append(content)
    if items is not None:
        segments.append(_encode_items_block(items))
    if image_url is not None:
        segments.append(_encode_image_block(image_url))
    return "\n\n".join(segments)


_BODY_MARKER_RE = re.compile(
    r"(?m)^[ \t]*<!--\s*odysseus-note:(?:items|image)\s*-->[ \t]*(?:\r?\n|$)"
)


def _remove_generated_separator(prefix: str) -> str:
    """Remove the one separator inserted between body and metadata blocks.

    ``_build_body`` joins segments with two newlines.  Removing only that
    separator (rather than calling ``strip``) preserves user-intended leading,
    trailing, and blank lines in the note body.
    """
    if prefix.endswith("\r\n\r\n"):
        return prefix[:-4]
    if prefix.endswith("\n\n"):
        return prefix[:-2]
    return prefix


def _parse_body(body: str, content_len: Optional[int], content_empty: bool = False):
    """Parse a note body without trusting the legacy length hint.

    ``x-content-len`` was emitted by the first migration implementation.  It
    is retained in old files for provenance, but it cannot be authoritative:
    editing the body in Obsidian makes it stale and slicing by it loses user
    content.  Metadata blocks are self-delimiting, so locate those structurally
    and treat all other body text as prose.
    """
    marker = _BODY_MARKER_RE.search(body)
    if marker:
        content_prefix = _remove_generated_separator(body[: marker.start()])
        content: Optional[str]
        if content_prefix:
            content = content_prefix
        elif content_empty:
            content = ""
        else:
            content = None
        remainder = body[marker.start():]
    else:
        # No metadata block means the entire body is human prose.  This also
        # makes an external edit to a legacy plain note lossless even when its
        # old x-content-len value is stale.
        content = body if body else ("" if content_empty else None)
        remainder = ""
    items = _extract_items_block(remainder)
    image_url = _extract_image_block(remainder)
    return content, items, image_url


# ---------------------------------------------------------------------------
# Timestamps — NoteRecord carries naive UTC datetimes (TimestampMixin's
# ``utcnow_naive`` convention); encode/decode both assume that and are
# symmetric about it so equality comparisons never trip on aware-vs-naive.
# ---------------------------------------------------------------------------


def _dt_to_iso(dt: datetime) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat() + "Z"


def _iso_to_dt(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day") and not isinstance(value, str):
        # PyYAML parses a bare `2026-08-14` as datetime.date.
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1]
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Frontmatter <-> NoteRecord
# ---------------------------------------------------------------------------


def _fm_get(fm: Dict[str, Any], key: str) -> Any:
    """Case-insensitive lookup — these files are meant to be hand-edited."""
    if key in fm:
        return fm[key]
    lowered = key.lower()
    for k, v in fm.items():
        if str(k).lower() == lowered:
            return v
    return None


def note_to_markdown(note: NoteRecord) -> str:
    """Render a :class:`NoteRecord` as a full Markdown file (frontmatter + body).

    Does not know about ``archived`` — that is expressed by where the caller
    writes the file (see :func:`resolve_note_directory`), not by anything in
    the file's own text.
    """
    import yaml

    fm: Dict[str, Any] = dict(note.extra_frontmatter or {})
    # Application-owned keys always reflect the current record.  Drop legacy
    # framing metadata even when it was present in an externally edited file.
    fm.pop("x-content-len", None)
    fm.update({"id": note.id, "title": note.title})
    if note.owner is not None:
        fm["owner"] = note.owner
    # Never encode a body length.  The file is deliberately editable in
    # Obsidian, so a cached character count would become stale on the first
    # external edit and could truncate the user's prose on the next read.
    if note.content == "":
        fm["x-content-empty"] = True
    if note.items is not None:
        fm["x-note-type"] = "checklist"
    if note.image_url is not None:
        # Image metadata is first-class frontmatter. Older files may still
        # carry the body marker; the decoder below accepts that form too.
        fm["image"] = note.image_url
    if note.due_date is not None:
        fm["due"] = note.due_date
    if note.repeat and note.repeat != "none":
        fm["repeat"] = note.repeat
    if note.label is not None:
        fm["tags"] = [note.label] if note.label else []
    if note.color is not None:
        fm["color"] = note.color
    if note.pinned:
        fm["pinned"] = True
    if note.source and note.source != "user":
        fm["source"] = note.source
    if note.session_id is not None:
        fm["session"] = note.session_id
    if note.agent_session_id is not None:
        fm["agent_session"] = note.agent_session_id
    if note.ai_classification is not None:
        fm["x-ai"] = note.ai_classification
    if note.ai_content_hash is not None:
        fm["x-ai-hash"] = note.ai_content_hash
    if note.sort_order:
        fm["order"] = note.sort_order
    if note.created_at is not None:
        fm["created"] = _dt_to_iso(note.created_at)
    if note.updated_at is not None:
        fm["updated"] = _dt_to_iso(note.updated_at)

    body = _build_body(note.content, note.items, None)

    # yaml.safe_dump always terminates its output with "\n", and
    # split_frontmatter's closing pattern swallows exactly one newline after
    # the closing "---" — so "---\n{header}---\n{body}" round-trips through
    # it to hand back *exactly* `body`, with no stray blank line shifting the
    # x-content-len slice on decode. Do not add a blank line here for
    # "readability"; it would silently corrupt that slice.
    header = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return f"---\n{header}---\n{body}"


def markdown_to_note(text: str) -> NoteRecord:
    """Inverse of :func:`note_to_markdown`. ``archived`` defaults to False —
    set it from the file's directory if that matters to the caller.

    A file with no ``id`` in frontmatter (e.g. a note a human created by hand
    in the vault) is adopted with a freshly minted id rather than rejected;
    everything downstream keys off the id in frontmatter from that point on.
    """
    fm, body = split_frontmatter(text or "")

    note_id = _fm_get(fm, "id")
    note_id = str(note_id) if note_id not in (None, "") else str(uuid.uuid4())

    # ``x-content-len`` is a legacy hint only.  Do not use it to slice: a
    # human editor can change the body without updating frontmatter.
    legacy_content_len = _fm_get(fm, "x-content-len")
    legacy_content_len = int(legacy_content_len) if isinstance(legacy_content_len, (int, float)) else None
    content_empty = bool(_fm_get(fm, "x-content-empty") or False)
    content, items, image_url = _parse_body(body, legacy_content_len, content_empty)
    image_value = _fm_get(fm, "image")
    if image_value is not None:
        image_url = str(image_value)

    label_val = _fm_get(fm, "tags")
    label: Optional[str] = None
    if isinstance(label_val, list):
        label = str(label_val[0]) if label_val else ""
    elif label_val is not None:
        label = str(label_val)

    order_val = _fm_get(fm, "order")
    try:
        sort_order = int(order_val) if order_val is not None else 0
    except (TypeError, ValueError):
        sort_order = 0

    owner_val = _fm_get(fm, "owner")
    due_val = _fm_get(fm, "due")
    source_val = _fm_get(fm, "source")
    session_val = _fm_get(fm, "session")
    agent_session_val = _fm_get(fm, "agent_session")
    ai_val = _fm_get(fm, "x-ai")
    ai_hash_val = _fm_get(fm, "x-ai-hash")
    color_val = _fm_get(fm, "color")
    known_keys = {
        "id", "title", "owner", "x-content-len", "x-content-empty",
        "x-note-type", "due", "repeat", "tags", "color", "pinned", "image",
        "source", "session", "agent_session", "x-ai", "x-ai-hash",
        "order", "created", "updated",
    }
    extra_frontmatter = {
        str(key): value for key, value in fm.items()
        if str(key).lower() not in known_keys
    }

    return NoteRecord(
        id=note_id,
        owner=None if owner_val is None else str(owner_val),
        title=str(_fm_get(fm, "title") or ""),
        content=content,
        items=items,
        color=None if color_val is None else str(color_val),
        label=label,
        pinned=bool(_fm_get(fm, "pinned") or False),
        archived=False,
        due_date=None if due_val is None else str(due_val),
        source=str(source_val) if source_val else "user",
        session_id=None if session_val is None else str(session_val),
        sort_order=sort_order,
        image_url=image_url,
        repeat=str(_fm_get(fm, "repeat")) if _fm_get(fm, "repeat") else "none",
        ai_classification=None if ai_val is None else str(ai_val),
        ai_content_hash=None if ai_hash_val is None else str(ai_hash_val),
        agent_session_id=None if agent_session_val is None else str(agent_session_val),
        created_at=_iso_to_dt(_fm_get(fm, "created")),
        updated_at=_iso_to_dt(_fm_get(fm, "updated")),
        extra_frontmatter=extra_frontmatter,
    )


# ---------------------------------------------------------------------------
# Filenames and directory placement
# ---------------------------------------------------------------------------
# The note's real identity is the ``id`` in frontmatter (see markdown_to_note
# above). The filename is purely a human-friendly label: it can be truncated,
# de-duplicated, or later renamed by the user in Obsidian without the note
# "moving" as far as this codec is concerned.

_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_UNSAFE_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DASH_RUN_RE = re.compile(r"-{2,}")
_MAX_STEM_LEN = 80


def slugify_title(title: str) -> str:
    """A filesystem-safe stem derived from a note title.

    Never a path: every separator (``/``, ``\\``) and every other
    Windows/POSIX-unsafe character is replaced outright, so a title like
    ``"../../etc/passwd"`` cannot escape the directory it is written into —
    it becomes an ordinary (if odd-looking) filename, never a relative path
    with meaning. This is belt-and-suspenders with the containment check in
    :func:`safe_join`; either alone would already stop traversal.
    """
    text = unicodedata.normalize("NFC", title or "").strip()
    text = _UNSAFE_CHARS_RE.sub("-", text)
    text = _DASH_RUN_RE.sub("-", text)
    text = text.strip(" .-")
    text = text[:_MAX_STEM_LEN].strip(" .-")
    if not text or set(text) <= {"."}:
        return ""
    if text.lower() in _WINDOWS_RESERVED:
        text = f"note-{text}"
    return text


def note_filename(title: str, note_id: str, existing: Optional[Set[str]] = None) -> str:
    """A ``.md`` filename for *title*, unique against *existing* (lowercased
    stems already used in the same target directory).

    Deterministic: the same ``(title, note_id, existing)`` always yields the
    same result, so re-running a migration over an unchanged vault reproduces
    the exact filenames it chose before instead of drifting.
    """
    existing = existing or set()
    base = slugify_title(title) or f"note-{note_id[:8]}"
    candidate = base
    n = 2
    while candidate.lower() in existing:
        candidate = f"{base}-{n}"
        n += 1
    return f"{candidate}.md"


def resolve_note_directory(archived: bool, notes_directory: str, notes_archive_directory: str) -> str:
    """The vault-relative folder a note belongs in, purely a function of its
    archived flag — ``archived`` is state expressed as *location*, per spec."""
    return notes_archive_directory if archived else notes_directory


def safe_join(vault_root: Path, relative_path: str) -> Optional[Path]:
    """Resolve *relative_path* under *vault_root*, or ``None`` if it would
    escape (a defence-in-depth check behind :func:`slugify_title`, which
    should already make this impossible for filenames this module generates,
    but any caller handing us an externally-influenced relative path benefits
    from the same guarantee the personal-directory routes use elsewhere in
    this codebase: resolve symlinks with realpath, then check containment).
    """
    root = Path(vault_root).resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate
