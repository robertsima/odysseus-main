"""One classifier for what a vault file is to the person using it.

The Library and the vault tree both ask this module; nothing else decides a
role. Everything here is pure: names and modification times in, roles and
groups out. Nothing is read from, moved in or deleted from the filesystem.

Roles (see "Artifact roles" in ``static/js/TASK_FIRST_UI.md``):

``signed``      executed copy, locked
``history``     earlier revision or backup
``supporting``  preview, receipt, hash, HTML render
``source``      image or original upload
``export``      copy made from the master (PDF, Word copy)
``master``      the editable copy that is the source of truth
``None``        a file type the contract does not classify

Precedence when several rules match: a history folder or backup marker wins
over everything (a backup of a signed copy is history), then ``signed``, then
``supporting``, ``source``, ``export``, ``master``.

Two readings of the contract worth knowing:

* ``signed`` matches ``signed``/``executed``/``countersigned`` as a whole
  word, so "Unsigned draft" and "Designed layout" are not locked.
* ``history`` also attaches to a group found anywhere under the nearest
  non-history ancestor when the same folder has none, because real review
  packets mirror a project instead of one folder.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROLES = ("master", "export", "signed", "source", "history", "supporting")

HISTORY_FOLDERS = frozenset({
    "backups", "backup", "archive", "archived", "history", "revisions",
    "old", "review packets", ".trash",
})
SUPPORTING_FOLDERS = frozenset({"previews", "receipts"})
SOURCE_FOLDERS = frozenset({"originals", "source", "uploads", "inbox"})

SUPPORTING_EXTS = frozenset({".html", ".htm", ".json", ".sha256", ".md5", ".txt"})
IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".avif",
})
MARKDOWN_EXTS = frozenset({".md", ".markdown"})
MASTER_EXTS = MARKDOWN_EXTS | {".docx"}

_KNOWN_EXTS = (
    MASTER_EXTS | SUPPORTING_EXTS | IMAGE_EXTS
    | {".pdf", ".pptx", ".xlsx", ".xls", ".epub", ".svg", ".bak", ".docm", ".doc", ".odt"}
)

_SIGNED_RE = re.compile(r"(?<![a-z])(?:signed|executed|countersigned)(?![a-z])", re.IGNORECASE)
_COPY_SUFFIX_RE = re.compile(r"\s*\(\d+\)$")

_DATE_TAIL = (
    r"(?:\d{8}(?:[t_ -]?\d{6}z?)?"
    r"|\d{4}-\d{2}-\d{2}(?:[t _-]\d{2}[-:]?\d{2}(?:[-:]?\d{2})?z?)?)"
)
# Each pattern removes one kind of trailing decoration; normalize_stem loops
# until none applies.
_TRAILING_RES = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"[\s_-]*\(\d+\)$",
    r"[\s_-]+copy(?:\s+\d+)?$",
    r"[\s_-]+v\d+$",
    r"[\s_-]+(?:countersigned|executed|signed)$",
    r"[\s_-]+" + _DATE_TAIL + r"$",
))
# Supporting files ("Agreement.preview.html") attach by the name they preview.
_SUPPORT_TAIL_RE = re.compile(
    r"[\s._-]+(?:preview|receipt|render|rendered|hash|checksum|sha256|md5)$", re.IGNORECASE
)


# --- names -----------------------------------------------------------------

def _split_path(path: str) -> Tuple[List[str], str]:
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return [], ""
    return parts[:-1], parts[-1]


def _ext(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def display_stem(name: str) -> str:
    """The file name without its (known) extension, case preserved."""
    ext = _ext(name)
    return name[: -len(ext)] if ext in _KNOWN_EXTS else name


def normalize_stem(name: str) -> str:
    """Key under which a master, its exports, copies and signed copies meet.

    Lowercase, extension removed, whitespace collapsed, and trailing ` v2`,
    ``-v3``, ` (1)`, ` copy`, ``_signed`` and dates/timestamps stripped.
    """
    stem = display_stem(str(name or "").strip())
    stem = re.sub(r"\s+", " ", stem).strip().lower()
    while True:
        for pattern in _TRAILING_RES:
            stripped = pattern.sub("", stem).rstrip(" ._-")
            if stripped and stripped != stem:
                stem = stripped
                break
        else:
            return stem


def _support_stem(name: str) -> str:
    stem = display_stem(name)
    stem = _SUPPORT_TAIL_RE.sub("", stem) or stem
    return normalize_stem(stem)


def is_locked_name(name: str) -> bool:
    """True for a signed/executed copy: shown locked, never edited."""
    return bool(_SIGNED_RE.search(display_stem(name)))


# --- classification --------------------------------------------------------

def history_folder_index(dirs: Iterable[str]) -> Optional[int]:
    """Index of the first history folder in ``dirs``, else None."""
    for index, part in enumerate(dirs):
        if part.lower() in HISTORY_FOLDERS:
            return index
    return None


def has_backup_marker(name: str) -> bool:
    lowered = name.lower()
    if lowered.endswith("~") or lowered.endswith(".bak") or ".bak." in lowered:
        return True
    return bool(_COPY_SUFFIX_RE.search(display_stem(name)))


def classify(path: str, siblings: Optional[Iterable[str]] = None, *, _markers: bool = True) -> Optional[str]:
    """Role of the file at vault-relative ``path``.

    ``siblings`` are the file names in the same folder; they decide whether a
    Word file is an export (a Markdown master with the same stem sits next to
    it). Without them a Word file is a master.
    """
    dirs, name = _split_path(path)
    if not name:
        return None
    lowered_dirs = [d.lower() for d in dirs]
    ext = _ext(name)

    if history_folder_index(dirs) is not None or (_markers and has_backup_marker(name)):
        return "history"
    if is_locked_name(name):
        return "signed"
    if ext in SUPPORTING_EXTS or SUPPORTING_FOLDERS.intersection(lowered_dirs):
        return "supporting"
    if ext in IMAGE_EXTS or SOURCE_FOLDERS.intersection(lowered_dirs):
        return "source"
    if ext == ".pdf":
        return "export"
    if ext == ".docx":
        stem = normalize_stem(name)
        for sibling in siblings or ():
            if _ext(sibling) in MARKDOWN_EXTS and normalize_stem(sibling) == stem:
                return "export"
        return "master"
    if ext in MARKDOWN_EXTS:
        return "master"
    return None


# --- grouping --------------------------------------------------------------

def _mtime(entry: Dict[str, Any]) -> float:
    try:
        return float(entry.get("mtime") or 0)
    except (TypeError, ValueError):
        return 0.0


def _newest_first(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(entries, key=_mtime, reverse=True)


def group_files(files: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fold a flat file list into groups, one per (folder, normalized stem).

    ``files`` are dicts with ``path`` (vault-relative, ``/`` separated) and
    ``mtime`` (epoch seconds). Each member is returned as a copy with ``role``
    set. A group is::

        {"key": "<folder>/<stem>", "folder", "stem", "title",
         "primary": member | None, "master", "current_export",
         "signed": [], "history": [], "supporting": [], "sources": [],
         "warnings": []}

    History members attach to the group with the same stem in the nearest
    non-history ancestor folder; history with no group to attach to is
    dropped, except a lone ``(1)`` copy, which becomes a group of its own.
    Supporting files with no matching group are dropped too: neither is ever
    a top-level item.
    """
    entries: List[Dict[str, Any]] = []
    names_by_folder: Dict[str, List[str]] = {}
    for raw in files:
        dirs, name = _split_path(raw.get("path", ""))
        if not name:
            continue
        folder = "/".join(dirs)
        entry = dict(raw)
        entry.update(path="/".join([*dirs, name]), name=name, folder=folder, dirs=dirs)
        entries.append(entry)
        names_by_folder.setdefault(folder, []).append(name)

    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def group_for(folder: str, stem: str) -> Dict[str, Any]:
        key = (folder, stem)
        if key not in groups:
            groups[key] = {
                "key": f"{folder}/{stem}", "folder": folder, "stem": stem, "title": "",
                "members": [], "history": [], "supporting_loose": [],
            }
        return groups[key]

    history_entries: List[Dict[str, Any]] = []
    for entry in entries:
        siblings = names_by_folder[entry["folder"]]
        role = classify(entry["path"], siblings)
        entry["role"] = role
        if role == "history":
            history_entries.append(entry)
        elif role == "supporting":
            entry["stem"] = _support_stem(entry["name"])
        else:
            entry["stem"] = normalize_stem(entry["name"])
            group_for(entry["folder"], entry["stem"])["members"].append(entry)

    # Supporting files join the group they preview, else the only group in
    # their folder.
    for entry in entries:
        if entry["role"] != "supporting":
            continue
        target = groups.get((entry["folder"], entry["stem"]))
        if target is None:
            in_folder = [g for (f, _s), g in groups.items() if f == entry["folder"] and g["members"]]
            target = in_folder[0] if len(in_folder) == 1 else None
        if target is not None:
            target["supporting_loose"].append(entry)

    stems_index: Dict[str, List[Dict[str, Any]]] = {}
    for group in groups.values():
        stems_index.setdefault(group["stem"], []).append(group)

    orphans: List[Dict[str, Any]] = []
    for entry in history_entries:
        entry["stem"] = normalize_stem(entry["name"])
        cut = history_folder_index(entry["dirs"])
        base = entry["dirs"] if cut is None else entry["dirs"][:cut]
        target = None
        for depth in range(len(base), -1, -1):
            target = groups.get(("/".join(base[:depth]), entry["stem"]))
            if target is not None and target["members"]:
                break
            target = None
        if target is None:
            prefix = "/".join(base)
            under = [
                g for g in stems_index.get(entry["stem"], [])
                if g["members"] and (not prefix or g["folder"] == prefix or g["folder"].startswith(prefix + "/"))
            ]
            target = under[0] if len(under) == 1 else None
        if target is not None:
            target["history"].append(entry)
        elif cut is None:
            orphans.append(entry)

    # A copy such as "Report (1).docx" with no original beside it is still
    # the only version of that file: show it as itself.
    for entry in orphans:
        entry["role"] = classify(entry["path"], names_by_folder[entry["folder"]], _markers=False)
        if entry["role"] in (None, "history", "supporting"):
            continue
        group_for(entry["folder"], entry["stem"])["members"].append(entry)

    result = [_finish(group) for group in groups.values() if group["members"]]
    return [g for g in result if g["primary"] is not None]


def _finish(group: Dict[str, Any]) -> Dict[str, Any]:
    members = group["members"]
    history = list(group["history"])

    masters = _newest_first([m for m in members if m["role"] == "master"])
    exports = _newest_first([m for m in members if m["role"] == "export"])
    signed = _newest_first([m for m in members if m["role"] == "signed"])
    sources = _newest_first([m for m in members if m["role"] == "source"])
    others = [m for m in members if m["role"] is None]

    master = masters[0] if masters else None
    history.extend(masters[1:])
    current_export = exports[0] if exports else None
    history.extend(exports[1:])

    warnings: List[str] = []
    if master is not None and _ext(master["name"]) in MARKDOWN_EXTS:
        for item in exports:
            if _ext(item["name"]) == ".docx" and _mtime(item) > _mtime(master):
                warnings.append("word_newer_than_master")
                break

    primary = master or (signed[0] if signed else None) or current_export \
        or (sources[0] if sources else None) or (others[0] if others else None)

    supporting = _newest_first(group["supporting_loose"])
    # Images beside a master are material for it, not a second item.
    if primary is not None and primary["role"] != "source":
        supporting = _newest_first(supporting + sources)
        sources = []
    elif primary is not None:
        supporting = _newest_first(supporting + sources[1:])

    title_source = primary["name"] if primary else group["stem"]
    return {
        "key": group["key"],
        "folder": group["folder"],
        "stem": group["stem"],
        "title": display_stem(title_source),
        "primary": primary,
        "master": master,
        "current_export": current_export,
        "signed": signed,
        "history": _newest_first(history),
        "supporting": supporting,
        "sources": sources,
        "warnings": warnings,
    }
