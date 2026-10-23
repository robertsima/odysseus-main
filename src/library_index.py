"""Builds the Library's items from the vault, documents and notes.

The vault walk uses ``scandir`` and ``stat`` only; no file is opened. Its
result is cached in-process per vault root for a short time, and the write
routes call :func:`clear`. Everything here is safe to call from a thread.
"""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence
from urllib.parse import quote

from src.artifact_roles import (
    display_stem,
    group_files,
    is_locked_name,
)

CACHE_TTL_SECONDS = 30.0
PROJECTS_FOLDER = "projects"
MAX_PROJECT_DEPTH = 4
_SKIP_DIRS = frozenset({"__pycache__", "node_modules"})

_clock: Callable[[], float] = time.monotonic
_lock = threading.RLock()
_cache: Dict[str, "_Scan"] = {}


class _Scan:
    __slots__ = ("expires", "files", "dirs")

    def __init__(self, expires: float, files: List[Dict[str, Any]], dirs: Dict[str, float]):
        self.expires = expires
        self.files = files
        self.dirs = dirs


def clear() -> None:
    """Forget every cached vault walk (called after vault and note writes)."""
    with _lock:
        _cache.clear()


# --- vault walk ------------------------------------------------------------

def _walk(root: str, file_kind: Callable[[str], Optional[str]], exclude: frozenset) -> _Scan:
    files: List[Dict[str, Any]] = []
    dirs: Dict[str, float] = {}
    stack = [(root, "")]
    while stack:
        directory, rel_dir = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name.startswith(".") or name in _SKIP_DIRS:
                continue
            rel = f"{rel_dir}/{name}" if rel_dir else name
            try:
                if entry.is_dir(follow_symlinks=False):
                    if rel.casefold() in exclude:
                        continue
                    dirs[rel] = entry.stat(follow_symlinks=False).st_mtime
                    stack.append((entry.path, rel))
                elif entry.is_file(follow_symlinks=False):
                    kind = file_kind(name)
                    if kind is None:
                        continue
                    stat = entry.stat(follow_symlinks=False)
                    files.append({
                        "path": rel, "name": name, "kind": kind,
                        "mtime": stat.st_mtime, "size": stat.st_size,
                    })
            except OSError:
                continue
    return _Scan(0.0, files, dirs)


def scan_vault(
    root: str,
    file_kind: Callable[[str], Optional[str]],
    exclude_dirs: Iterable[str] = (),
) -> _Scan:
    """Cached listing of every listable file and folder under ``root``."""
    exclude = frozenset(str(d).strip("/").replace("\\", "/").casefold() for d in exclude_dirs if d)
    key = f"{os.path.realpath(root)}\0{sorted(exclude)}"
    with _lock:
        hit = _cache.get(key)
        if hit is not None and hit.expires > _clock():
            return hit
        scan = _walk(root, file_kind, exclude)
        scan.expires = _clock() + CACHE_TTL_SECONDS
        _cache[key] = scan
        return scan


# --- projects --------------------------------------------------------------

def _depth_ok(rel: str) -> bool:
    return rel.count("/") + 1 <= MAX_PROJECT_DEPTH


def project_dirs(scan: _Scan) -> List[str]:
    """Vault paths of every project folder (a child of a ``Projects`` folder)."""
    found = []
    for rel in scan.dirs:
        parent, _, _name = rel.rpartition("/")
        if parent and parent.rsplit("/", 1)[-1].casefold() == PROJECTS_FOLDER and _depth_ok(parent):
            found.append(rel)
    return sorted(found, key=str.casefold)


def _projects_container(rel_dir: str, scan: _Scan) -> Optional[int]:
    """Index of the first ``Projects`` segment of ``rel_dir`` that is a real folder."""
    parts = rel_dir.split("/") if rel_dir else []
    for index, part in enumerate(parts[:MAX_PROJECT_DEPTH]):
        if part.casefold() == PROJECTS_FOLDER and "/".join(parts[: index + 1]) in scan.dirs:
            return index
    return None


def locate(rel_dir: str, scan: _Scan) -> tuple[Optional[str], List[str]]:
    """(project name or None, breadcrumb) for a vault folder.

    Inside a ``Projects`` folder the breadcrumb starts at that folder; elsewhere
    it starts at the vault root.
    """
    parts = rel_dir.split("/") if rel_dir else []
    index = _projects_container(rel_dir, scan)
    if index is None:
        return None, parts
    project = parts[index + 1] if len(parts) > index + 1 else None
    return project, parts[index:]


def _punct_key(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").casefold()))


def project_for_title(title: str, names: Sequence[str]) -> Optional[str]:
    """The project whose name the title starts with (longest wins)."""
    key = _punct_key(title)
    best = None
    for name in names:
        pk = _punct_key(name)
        if pk and (key == pk or key.startswith(pk + " ")) and (best is None or len(pk) > len(_punct_key(best))):
            best = name
    return best


_PROJECT_TAG_RE = re.compile(r"(?<![\w/])#?project/([^\s#,\]]+)", re.IGNORECASE)


def project_for_note(label: Optional[str], tags: Any, content: Optional[str], names: Sequence[str]) -> Optional[str]:
    """A note belongs to a project by its label or a ``#project/<name>`` tag."""
    by_key = {_punct_key(n): n for n in names if _punct_key(n)}
    if label and _punct_key(label) in by_key:
        return by_key[_punct_key(label)]
    candidates: List[str] = []
    if isinstance(tags, str):
        tags = re.split(r"[,\s]+", tags)
    for tag in tags or []:
        match = _PROJECT_TAG_RE.search(str(tag))
        if match:
            candidates.append(match.group(1))
    if content:
        candidates.extend(m.group(1) for m in _PROJECT_TAG_RE.finditer(content))
    for candidate in candidates:
        if _punct_key(candidate) in by_key:
            return by_key[_punct_key(candidate)]
    return None


def project_summaries(scan: _Scan, counts: Optional[Dict[str, Dict[str, int]]] = None) -> List[Dict[str, Any]]:
    """One entry per project name; same-named folders under several ``Projects`` merge."""
    merged: Dict[str, Dict[str, Any]] = {}
    for rel in project_dirs(scan):
        name = rel.rsplit("/", 1)[-1]
        entry = merged.setdefault(name.casefold(), {
            "name": name, "path": rel, "counts": {"documents": 0, "files": 0, "notes": 0},
            "_mtime": 0.0, "_dir_mtime": 0.0,
        })
        entry["_dir_mtime"] = max(entry["_dir_mtime"], scan.dirs.get(rel, 0.0))
    for f in scan.files:
        project, _crumb = locate(f["path"].rpartition("/")[0], scan)
        entry = merged.get(project.casefold()) if project else None
        if entry is not None:
            entry["_mtime"] = max(entry["_mtime"], f["mtime"])
    out = []
    for key, entry in sorted(merged.items()):
        extra = (counts or {}).get(key, {})
        entry["counts"].update({k: v for k, v in extra.items() if k in entry["counts"]})
        # A folder's own mtime moves with every rename inside it; use it only
        # for a project that has no files yet.
        mtime = entry.pop("_mtime") or entry.pop("_dir_mtime")
        entry.pop("_dir_mtime", None)
        entry["updated_at"] = iso_from_epoch(mtime) if mtime else None
        out.append(entry)
    return out


# --- items -----------------------------------------------------------------

def iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def iso_from_naive_utc(value: Optional[datetime]) -> Optional[str]:
    return (value.isoformat() + "Z") if value else None


def _file_links(path: str, kind: str) -> Dict[str, str]:
    if kind == "supporting":
        return {}
    q = quote(path, safe="/")
    links = {
        "open": f"/api/personal/vault/raw?path={q}",
        "download": f"/api/personal/vault/raw?path={q}&download=1",
    }
    if kind == "markdown":
        links["open"] = links["edit"] = f"/api/personal/vault/file?path={q}"
    return links


def file_item(entry: Dict[str, Any], scan: _Scan) -> Dict[str, Any]:
    folder = entry["path"].rpartition("/")[0]
    project, crumb = locate(folder, scan)
    role = entry.get("role")
    return {
        "id": f"file:{entry['path']}",
        "kind": "file",
        "title": display_stem(entry["name"]),
        "type": "other" if entry.get("kind") == "supporting" else entry.get("kind"),
        "role": role,
        "project": project,
        "breadcrumb": crumb,
        "path": entry["path"],
        "updated_at": iso_from_epoch(entry["mtime"]),
        "locked": is_locked_name(entry["name"]),
        "links": _file_links(entry["path"], entry.get("kind") or ""),
    }


def group_item(group: Dict[str, Any], scan: _Scan) -> Dict[str, Any]:
    primary = group["primary"]
    base = file_item(primary, scan)
    members = [m for m in (group["master"], group["current_export"], *group["signed"], *group["supporting"], *group["sources"]) if m]
    newest = max([primary["mtime"], *(m["mtime"] for m in members)])
    editable = (
        primary["role"] == "master"
        and primary.get("kind") == "markdown"
        and not base["locked"]
    )
    related = bool(members and len(members) > 1) or bool(group["history"]) or base["project"] is not None
    actions = ["open"]
    if editable:
        actions.append("edit")
    actions.append("download")
    if group["history"]:
        actions.append("history")
    if related:
        actions.append("related")
    base.update(
        id=f"group:{group['key']}",
        kind="group",
        title=group["title"],
        updated_at=iso_from_epoch(newest),
        group={
            "master": file_item(group["master"], scan) if group["master"] else None,
            "current_export": file_item(group["current_export"], scan) if group["current_export"] else None,
            "signed": [file_item(m, scan) for m in group["signed"]],
            "history": [file_item(m, scan) for m in group["history"]],
            "supporting": [file_item(m, scan) for m in group["supporting"]],
            "warnings": list(group["warnings"]),
        },
        actions=actions,
    )
    return base


def vault_group_items(scan: _Scan) -> List[Dict[str, Any]]:
    return [group_item(g, scan) for g in group_files(scan.files)]


def document_item(row: Any, project: Optional[str]) -> Dict[str, Any]:
    return {
        "id": f"doc:{row.id}",
        "kind": "document",
        "title": row.title or "Untitled",
        "type": "pdf" if getattr(row, "is_pdf", False) else "document",
        "role": None,
        "project": project,
        "breadcrumb": ["Projects", project] if project else ["Documents"],
        "path": None,
        "updated_at": iso_from_naive_utc(row.updated_at),
        "locked": False,
        "language": row.language,
        "actions": ["open", "edit", "history", "related"],
    }


def note_item(note: Any, project: Optional[str]) -> Dict[str, Any]:
    return {
        "id": f"note:{note.id}",
        "kind": "note",
        "title": note.title or "Untitled note",
        "type": "note",
        "role": None,
        "project": project,
        "breadcrumb": ["Projects", project] if project else ["Notes"],
        "path": None,
        "updated_at": iso_from_naive_utc(note.updated_at),
        "locked": False,
        "actions": ["open", "edit", "related"],
    }


def matches_query(item: Dict[str, Any], query: str) -> bool:
    """Every whitespace-separated token appears in the title or breadcrumb."""
    tokens = [t for t in str(query or "").casefold().split() if t]
    if not tokens:
        return True
    haystack = " ".join([item.get("title") or "", *item.get("breadcrumb", []), item.get("project") or ""]).casefold()
    return all(t in haystack for t in tokens)
