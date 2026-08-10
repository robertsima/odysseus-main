"""Filesystem containment, hashing, and log-redaction primitives.

Everything here treats its input as hostile: paths come from MCP clients,
filenames and cell values come from files the user dropped into a directory.
"""

from __future__ import annotations

import hashlib
import logging
import ntpath
import posixpath
import re
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath

__all__ = [
    "PathSecurityError",
    "csv_safe_cell",
    "hash_file",
    "hash_note",
    "install_note_safe_logging",
    "resolve_within_root",
    "sanitize_filename",
]

_HASH_CHUNK = 1024 * 1024

# Windows reserves these device names regardless of extension.
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Characters that are illegal on Windows or ambiguous in logs.
_UNSAFE_FILENAME_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')

# A leading =, +, -, @ (or the tab/CR variants Excel also honours) makes a cell
# executable when opened in a spreadsheet.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class PathSecurityError(Exception):
    """Raised when a client-supplied path escapes the approved import root."""


def _is_absolute_anywhere(raw: str) -> bool:
    """True if ``raw`` looks absolute under POSIX *or* Windows rules.

    The server may run on Linux while the client sends a Windows path (or vice
    versa); a path that is absolute under either OS is rejected on both.
    """
    if posixpath.isabs(raw) or ntpath.isabs(raw):
        return True
    # ntpath.isabs("C:foo") is False but the drive-relative form still escapes
    # the root, and UNC prefixes must never be accepted.
    if PureWindowsPath(raw).drive or raw.startswith(("\\\\", "//")):
        return True
    return False


def resolve_within_root(root: Path, relative: str) -> Path:
    """Resolve ``relative`` against ``root`` and prove the result stays inside.

    Rejects absolute paths, drive letters, UNC prefixes, ``..`` traversal, NUL
    bytes, and symlinks that point outside the root. Containment is verified
    *after* full symlink resolution, so a link inside the root that targets an
    outside file is rejected too.
    """
    if not isinstance(relative, str) or not relative.strip():
        raise PathSecurityError("A relative path inside the import root is required.")
    if "\x00" in relative:
        raise PathSecurityError("Path contains a NUL byte.")
    if _is_absolute_anywhere(relative):
        raise PathSecurityError(
            "Absolute paths are not accepted; use a path inside the import root."
        )

    # Normalize separators before inspecting the parts so "..\\x" is caught on
    # POSIX hosts as well.
    parts = PurePosixPath(relative.replace("\\", "/")).parts
    if any(part == ".." for part in parts):
        raise PathSecurityError("Parent-directory traversal ('..') is not allowed.")

    real_root = Path(root).resolve(strict=False)
    candidate = (real_root / PurePosixPath(*parts)).resolve(strict=False)

    # Path.resolve() follows symlinks, so this single check covers both textual
    # traversal that survived normalization and link-based escape.
    if candidate != real_root and real_root not in candidate.parents:
        raise PathSecurityError("Resolved path escapes the approved import root.")
    return candidate


def sanitize_filename(name: str, *, max_length: int = 120) -> str:
    """Reduce a filename to something safe to store and echo back in a response.

    Only the basename survives; directory components, control characters, and
    Windows-reserved device names are stripped. The result never reveals the
    host directory an import came from.
    """
    base = PurePosixPath(str(name).replace("\\", "/")).name
    base = unicodedata.normalize("NFC", base)
    base = _UNSAFE_FILENAME_CHARS.sub("_", base).strip(" .")
    if not base:
        return "unnamed"
    stem, dot, suffix = base.partition(".")
    if stem.upper() in _WINDOWS_RESERVED:
        stem = f"_{stem}"
    base = f"{stem}{dot}{suffix}"
    if len(base) > max_length:
        head, _, tail = base.rpartition(".")
        base = (head[: max_length - len(tail) - 1] + "." + tail) if head else base[:max_length]
    return base


def hash_file(path: Path) -> str:
    """SHA-256 of a file's bytes, used to recognise an exact repeated import."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_note(note: str | None) -> str:
    """Stable digest of a journal note.

    Fingerprints, logs, and diagnostics use this instead of the note text, so
    two exports of the same entry collide without the note ever leaving the
    database. Normalized to NFC so the same text typed on different platforms
    hashes identically.
    """
    if note is None:
        return ""
    return hashlib.sha256(unicodedata.normalize("NFC", note).encode("utf-8")).hexdigest()


def csv_safe_cell(value: str) -> str:
    """Neutralize spreadsheet formula injection in cells this app *writes*.

    Imported values are never executed, but an admin CSV export of emotion
    labels could otherwise carry ``=HYPERLINK(...)`` into Excel. Prefixing with
    a single quote keeps the text readable while disarming the formula.
    """
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_FORMULA_PREFIXES) else text


class _NoteSafeFilter(logging.Filter):
    """Drop any log record that was explicitly tagged as carrying note text.

    Call sites are expected not to log notes at all; this is the backstop that
    turns a mistake into a dropped record rather than a privacy incident.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, "contains_note", False)


def install_note_safe_logging(logger: logging.Logger) -> logging.Logger:
    """Attach the note-safety filter to ``logger`` exactly once."""
    if not any(isinstance(f, _NoteSafeFilter) for f in logger.filters):
        logger.addFilter(_NoteSafeFilter())
    return logger
