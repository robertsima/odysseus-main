"""Incremental re-index of tracked personal-document directories.

Without this, a vault indexed once goes stale the moment anything writes to it —
the AI updating its own notes, or you saving in Obsidian on the host. RAG keeps
serving the old chunk and there is no signal that it is wrong.

Change detection is by (mtime, size) rather than a filesystem watcher, for two
reasons: no watcher dependency is installed, and inotify events do not propagate
from a Docker Desktop host bind mount into the container, so a watcher would
silently miss exactly the case that matters most (saves made outside the app).
Polling a metadata stat per file is cheap; only changed files are re-embedded.

State lives in a dot-prefixed file inside PERSONAL_DIR so ``index_walk`` skips
it — it must never be indexed as a document itself.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.index_walk import is_indexable_file, prune_index_dirs
from src.rag_sensitivity import SENSITIVITY_PUBLIC

logger = logging.getLogger(__name__)

STATE_FILENAME = ".vault_scan_state.json"

# Default gap between automatic scans. Short enough that a save shows up in
# retrieval while you are still working, long enough that a large vault is not
# being stat-walked constantly.
DEFAULT_SCAN_INTERVAL_S = 30

_scan_task = None


class VaultScanner:
    """Re-indexes only the files whose (mtime, size) changed since last scan."""

    def __init__(self, personal_docs_manager, rag_manager):
        self.manager = personal_docs_manager
        self.rag = rag_manager
        self._state_path = os.path.join(personal_docs_manager.personal_dir, STATE_FILENAME)
        self._state: Dict[str, list] = {}
        self._load_state()

    # -- state -------------------------------------------------------------

    def _load_state(self) -> None:
        try:
            if os.path.exists(self._state_path):
                with open(self._state_path, "r", encoding="utf-8") as handle:
                    stored = json.load(handle)
                if isinstance(stored, dict):
                    self._state = {
                        k: list(v) for k, v in stored.items()
                        if isinstance(k, str) and isinstance(v, (list, tuple)) and len(v) == 2
                    }
        except Exception as e:
            logger.warning("Could not load vault scan state (%s); treating as first run", e)
            self._state = {}

    def _save_state(self) -> None:
        try:
            with open(self._state_path, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle)
        except Exception as e:
            logger.warning("Could not save vault scan state: %s", e)

    # -- scanning ----------------------------------------------------------

    def _tracked_directories(self) -> list:
        dirs = [self.manager.personal_dir]
        dirs.extend(self.manager.get_indexed_directories())
        seen = set()
        unique = []
        for directory in dirs:
            key = os.path.abspath(directory)
            if key not in seen and os.path.isdir(key):
                seen.add(key)
                unique.append(key)
        return unique

    def _current_files(self, extensions) -> Dict[str, Tuple[float, int]]:
        found: Dict[str, Tuple[float, int]] = {}
        for directory in self._tracked_directories():
            for root, subdirs, names in os.walk(directory):
                prune_index_dirs(subdirs)
                for name in names:
                    if not is_indexable_file(name):
                        continue
                    if Path(name).suffix.lower() not in extensions:
                        continue
                    path = os.path.abspath(os.path.join(root, name))
                    try:
                        stat = os.stat(path)
                    except OSError:
                        continue
                    found[path] = (stat.st_mtime, stat.st_size)
        return found

    def scan(self, extensions=None) -> Dict[str, Any]:
        """Re-index changed/new files and drop chunks for deleted ones."""
        if not self.rag:
            return {"scanned": 0, "reindexed": 0, "removed": 0, "skipped": "no rag manager"}

        if extensions is None:
            from src.rag_vector import DEFAULT_FILE_EXTENSIONS
            extensions = DEFAULT_FILE_EXTENSIONS

        current = self._current_files(extensions)
        previous = self._state

        changed = [p for p, sig in current.items() if list(sig) != previous.get(p)]
        removed = [p for p in previous if p not in current]

        reindexed = 0
        for path in changed:
            directory = os.path.dirname(path)
            sensitivity = self._sensitivity_for(path)
            owner = self._owner_for(directory)
            try:
                # Delete first: chunk ids are content-derived, so an edited file
                # would otherwise leave its previous chunks behind as orphans
                # that still match searches.
                self.rag.delete_by_source(path)
                indexed, _failed = self.rag.index_file(path, owner=owner, sensitivity=sensitivity)
                if indexed:
                    reindexed += 1
            except Exception as e:
                logger.warning("re-index failed for %s: %s", path, e)
                continue
            self._state[path] = list(current[path])

        for path in removed:
            try:
                self.rag.delete_by_source(path)
            except Exception as e:
                logger.warning("chunk cleanup failed for %s: %s", path, e)
                continue
            self._state.pop(path, None)

        if changed or removed:
            self._save_state()
            try:
                self.manager.refresh_index()
            except Exception as e:
                logger.warning("keyword index refresh failed after scan: %s", e)
            logger.info(
                "Vault scan: %s file(s) re-indexed, %s removed, %s tracked",
                reindexed, len(removed), len(current),
            )

        return {
            "scanned": len(current),
            "reindexed": reindexed,
            "removed": len(removed),
            "changed_files": [os.path.basename(p) for p in changed[:20]],
        }

    # -- per-file attributes ----------------------------------------------

    def _sensitivity_for(self, path: str) -> str:
        resolver = getattr(self.manager, "sensitivity_for", None)
        if callable(resolver):
            try:
                return resolver(path)
            except Exception:
                pass
        return SENSITIVITY_PUBLIC

    def _owner_for(self, directory: str) -> Optional[str]:
        resolver = getattr(self.rag, "owner_for_directory", None)
        if callable(resolver):
            try:
                return resolver(directory)
            except Exception:
                pass
        return None


def _interval_seconds() -> int:
    raw = os.environ.get("ODYSSEUS_VAULT_SCAN_SECONDS")
    if raw is None or not str(raw).strip():
        return DEFAULT_SCAN_INTERVAL_S
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        logger.warning("Ignoring non-numeric ODYSSEUS_VAULT_SCAN_SECONDS=%r", raw)
        return DEFAULT_SCAN_INTERVAL_S
    if value <= 0:
        return 0  # explicit opt-out
    return max(value, 5)


async def _scan_loop(scanner: VaultScanner, interval: int) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            # Walking + embedding is blocking work; keep it off the event loop
            # so it cannot stall request handling on a large vault.
            await asyncio.to_thread(scanner.scan)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("vault scan tick failed: %s", e)


def start_vault_scanner(personal_docs_manager, rag_manager):
    """Start the periodic scan. Returns the task, or None when disabled."""
    global _scan_task

    interval = _interval_seconds()
    if not interval:
        logger.info("Vault scanner disabled (ODYSSEUS_VAULT_SCAN_SECONDS=0)")
        return None
    if not personal_docs_manager or not rag_manager:
        logger.info("Vault scanner not started (no personal docs / RAG manager)")
        return None

    scanner = VaultScanner(personal_docs_manager, rag_manager)
    _scan_task = asyncio.create_task(_scan_loop(scanner, interval))
    logger.info("Vault scanner started (every %ss)", interval)
    return _scan_task
