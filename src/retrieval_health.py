"""Is the document index describing the files that exist right now?

The 2026-09-21 logs show both halves of a split-brain index: every declared
personal directory ("Vault Mind", "AI Mind", "Journal") was "not a directory
— skipped" because its mount was absent, while ChromaDB still answered with
over a thousand chunks indexed from those same trees earlier. Retrieval kept
returning that old material as if it were the user's current vault, and a
planning agent had no way to tell.

This module compares the configured sources with what is on disk and with
what the vector store holds, per source, and says whether indexed context can
be presented as current. It only reads; it never reindexes or deletes.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# Bounds so a health check can never become a full crawl of a large vault.
MAX_FILES_PER_SOURCE = 20000
VECTOR_PAGE = 2000
MAX_VECTOR_ROWS = 200000


def _scan(directory: str) -> Dict[str, Any]:
    count, newest, truncated = 0, 0.0, False
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            count += 1
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, name)))
            except OSError:
                pass
            if count >= MAX_FILES_PER_SOURCE:
                truncated = True
                break
        if truncated:
            break
    return {"file_count": count, "file_count_truncated": truncated,
            "newest_file_mtime": newest or None}


def _declared_sources(personal_dir: Optional[str]) -> List[Dict[str, str]]:
    """Directories named in ODYSSEUS_PERSONAL_DIRS, including missing ones.

    A declared tree whose mount is absent is skipped at startup and never
    tracked, so it is invisible everywhere else; this is where it shows up.
    """
    try:
        from src.personal_dirs_config import ENV_VAR, parse_declarations, resolve_declared_path
    except Exception:
        return []
    raw = os.environ.get(ENV_VAR) or ""
    if not raw.strip() or not personal_dir:
        return []
    entries, _errors = parse_declarations(raw)
    out = []
    for entry in entries:
        resolved = resolve_declared_path(entry.path, personal_dir) or entry.path
        out.append({"directory": os.path.abspath(resolved), "sensitivity": entry.sensitivity,
                    "declared": True})
    return out


def _vector_rows(rag) -> Iterable[Dict[str, Any]]:
    seen = 0
    for lane_name, collection in (rag._active_collections() if rag is not None else []):
        offset = 0
        while seen < MAX_VECTOR_ROWS:
            try:
                page = collection.get(include=["metadatas"], limit=VECTOR_PAGE, offset=offset)
            except TypeError:  # older clients: no paging
                page = collection.get(include=["metadatas"])
                offset = None
            metas = page.get("metadatas") or []
            for meta in metas:
                seen += 1
                yield {"lane": lane_name, **(meta if isinstance(meta, dict) else {})}
            if offset is None or len(metas) < VECTOR_PAGE:
                break
            offset += VECTOR_PAGE


def _source_for(path: str, sources: List[str]) -> Optional[str]:
    best = None
    for directory in sources:
        if path == directory or path.startswith(directory.rstrip(os.sep) + os.sep):
            if best is None or len(directory) > len(best):
                best = directory
    return best


def retrieval_health(personal_docs_manager=None, rag=None, *, allow_private: bool = False,
                     scan_vectors: bool = True, now: Optional[float] = None) -> Dict[str, Any]:
    """Per-source mount, file and vector state, and an overall verdict.

    ``allow_private=False`` names a private source only by its label: a path
    is itself disclosure. Counts are still reported, so the verdict is the
    same either way.
    """
    from src.constants import DATA_DIR

    now = time.time() if now is None else now
    if personal_docs_manager is None or rag is None:
        try:
            from src import ai_interaction

            personal_docs_manager = personal_docs_manager or ai_interaction._personal_docs_manager
            rag = rag or ai_interaction._rag_manager
        except Exception:
            pass
    personal_dir = getattr(personal_docs_manager, "personal_dir", None)
    labels = dict(getattr(personal_docs_manager, "directory_sensitivity", {}) or {})

    rows: Dict[str, Dict[str, Any]] = {}

    def add(directory, sensitivity, **extra):
        directory = os.path.abspath(directory)
        row = rows.setdefault(directory, {"directory": directory, "sensitivity": sensitivity,
                                          "tracked": False, "declared": False})
        row.update(extra)

    if personal_dir:
        add(personal_dir, labels.get(os.path.abspath(personal_dir), "public"), tracked=True, base=True)
    for directory in getattr(personal_docs_manager, "indexed_directories", None) or []:
        add(directory, labels.get(os.path.abspath(directory), "public"), tracked=True)
    for row in _declared_sources(personal_dir):
        add(row["directory"], row["sensitivity"], declared=True)

    local_counts: Dict[str, int] = {}
    for doc in getattr(personal_docs_manager, "index", None) or []:
        src = os.path.abspath(str(doc.get("source_dir") or ""))
        local_counts[src] = local_counts.get(src, 0) + 1

    for row in rows.values():
        directory = row["directory"]
        row["exists"] = os.path.isdir(directory)
        row.update(_scan(directory) if row["exists"] else
                   {"file_count": 0, "file_count_truncated": False, "newest_file_mtime": None})
        row["listed_documents"] = local_counts.get(directory, 0)
        row["chunk_count"] = 0
        row["lanes"] = {}

    vector_state: Dict[str, Any] = {"available": rag is not None and bool(getattr(rag, "healthy", False))}
    orphaned = unattributed = 0
    owners: Dict[str, set] = {}
    if scan_vectors and vector_state["available"]:
        try:
            stats = rag.get_stats() or {}
            vector_state.update({"document_count": stats.get("document_count"),
                                 "lanes": stats.get("embedding_lanes"),
                                 "persist_directory": stats.get("persist_directory")})
        except Exception as exc:
            vector_state["stats_error"] = str(exc)[:200]
        source_dirs = sorted(rows)
        exists_cache: Dict[str, bool] = {}
        try:
            for meta in _vector_rows(rag):
                path = str(meta.get("source") or "")
                owner_dir = _source_for(os.path.abspath(path), source_dirs) if path else None
                if owner_dir is None:
                    unattributed += 1
                    continue
                row = rows[owner_dir]
                row["chunk_count"] += 1
                row["lanes"][meta["lane"]] = row["lanes"].get(meta["lane"], 0) + 1
                if meta.get("owner"):
                    owners.setdefault(owner_dir, set()).add(str(meta["owner"]))
                if path not in exists_cache:
                    exists_cache[path] = os.path.exists(path)
                if not exists_cache[path]:
                    row["orphaned_chunks"] = row.get("orphaned_chunks", 0) + 1
                    orphaned += 1
        except Exception as exc:
            vector_state["scan_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"

    problems: List[str] = []
    if not vector_state["available"]:
        problems.append("the vector store is not available")
    for directory, row in rows.items():
        row["owners"] = sorted(owners.get(directory, set()))
        if not row["exists"]:
            row["status"] = "stale_vectors" if row["chunk_count"] else "missing_mount"
        elif row.get("orphaned_chunks"):
            row["status"] = "stale_vectors"
        elif row["file_count"] and not row["chunk_count"] and vector_state["available"] and scan_vectors:
            row["status"] = "not_indexed"
        elif not row["file_count"]:
            row["status"] = "empty"
        else:
            row["status"] = "ok"
        if row["status"] not in {"ok", "empty"}:
            label = directory if allow_private or row["sensitivity"] != "private" else "a private source"
            problems.append({
                "missing_mount": f"{label} is not mounted (declared but not a directory)",
                "stale_vectors": (f"{label} has {row['chunk_count']} indexed chunk(s)"
                                  + (" but its directory is missing" if not row["exists"]
                                     else f", {row.get('orphaned_chunks', 0)} from files that no longer exist")),
                "not_indexed": f"{label} has {row['file_count']} file(s) and no indexed chunks",
            }[row["status"]])
        if not allow_private and row["sensitivity"] == "private":
            row["directory"] = "(private source)"
            row["owners"] = []

    sources = sorted(rows.values(), key=lambda r: (r["status"] == "ok", r["directory"]))
    trustworthy = vector_state["available"] and not problems
    return {
        "checked_at": now,
        "data_dir": DATA_DIR,
        "personal_dir": personal_dir if allow_private or labels.get(os.path.abspath(personal_dir or ""), "public") != "private" else "(private)",
        "status": "healthy" if trustworthy else ("unavailable" if not vector_state["available"] else "degraded"),
        "current_context_trustworthy": trustworthy,
        "problems": problems,
        "sources": sources,
        "vector_store": {**vector_state, "orphaned_chunks": orphaned, "unattributed_chunks": unattributed},
    }


# A cached verdict for the hot path (search_documents runs every few turns).
_CACHE: Dict[str, Any] = {}
_CACHE_SECONDS = 300


def cached_problems() -> List[str]:
    """The current problems, recomputed at most every few minutes. Never raises."""
    now = time.time()
    if _CACHE.get("at", 0) + _CACHE_SECONDS > now:
        return list(_CACHE.get("problems") or [])
    try:
        problems = list(retrieval_health(allow_private=False)["problems"])
    except Exception:
        logger.debug("retrieval health check failed", exc_info=True)
        problems = []
    _CACHE.update(at=now, problems=problems)
    return problems
