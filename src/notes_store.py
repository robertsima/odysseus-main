"""File-backed Notes repository inside the configured Markdown vault."""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from src.notes_markdown import (
    NoteRecord,
    markdown_to_note,
    note_filename,
    note_to_markdown,
    resolve_note_directory,
    safe_join,
)
from src.rag_sensitivity import vault_root
from src.settings import get_setting


def _paths() -> Tuple[Path, Path, Path]:
    root = Path(vault_root()).resolve()
    active = safe_join(root, str(get_setting("notes_directory", "Notes") or "Notes"))
    archive = safe_join(root, str(get_setting("notes_archive_directory", "Notes/Archive") or "Notes/Archive"))
    if active is None or archive is None:
        raise ValueError("notes directories must stay inside the vault")
    return root, active, archive


def _belongs(note: NoteRecord, owner: Optional[str]) -> bool:
    return owner is None or note.owner == owner


class MarkdownNotesStore:
    def _iter(self) -> Iterable[Tuple[Path, NoteRecord]]:
        root, active, archive = _paths()
        seen: set[Path] = set()
        # Archive commonly lives under Notes/. Scan it first so the active
        # directory's recursive walk cannot misclassify archived files.
        for directory, archived in ((archive, True), (active, False)):
            if not directory.is_dir():
                continue
            candidates = sorted({*directory.rglob("*.md"), *directory.rglob("*.markdown")})
            for path in candidates:
                resolved = path.resolve()
                # ``rglob`` can encounter symlinked files.  The notes store
                # is an API surface, so do not let a symlink inside the vault
                # turn it into a reader for arbitrary host Markdown files.
                try:
                    resolved.relative_to(root)
                except ValueError:
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)
                try:
                    text = path.read_text(encoding="utf-8")
                    note = markdown_to_note(text)
                    # A hand-authored Markdown file may not have an id yet.
                    # Give it a stable path-derived identity for reads; the id
                    # is persisted naturally on the first application edit.
                    from src.vault_markdown import split_frontmatter
                    frontmatter, _ = split_frontmatter(text)
                    if not frontmatter.get("id"):
                        note.id = str(uuid.uuid5(uuid.NAMESPACE_URL, resolved.as_posix()))
                    note.archived = archived
                    stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(tzinfo=None)
                    if note.created_at is None:
                        note.created_at = stamp
                    note.updated_at = stamp
                    yield path, note
                except (OSError, UnicodeError, ValueError):
                    continue

    def list(self, owner: Optional[str] = None, *, archived: bool = False,
             label: Optional[str] = None) -> List[NoteRecord]:
        notes = [
            note for _path, note in self._iter()
            if note.archived == archived and _belongs(note, owner)
            and (not label or note.label == label)
        ]
        if archived:
            return sorted(notes, key=lambda n: n.updated_at or datetime.min, reverse=True)
        return sorted(
            notes,
            key=lambda n: (not n.pinned, n.sort_order, -(n.updated_at or datetime.min).timestamp()),
        )

    def find(self, note_id: str, owner: Optional[str] = None) -> Optional[NoteRecord]:
        note_id = str(note_id or "").strip()
        if not note_id:
            return None
        matches = [note for _path, note in self._iter() if note.id.startswith(note_id) and _belongs(note, owner)]
        return matches[0] if len(matches) == 1 else None

    def _path_for(self, note_id: str, owner: Optional[str] = None) -> Optional[Path]:
        for path, note in self._iter():
            if note.id == note_id and _belongs(note, owner):
                return path
        return None

    def save(self, note: NoteRecord) -> NoteRecord:
        root, _active, _archive = _paths()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if note.created_at is None:
            note.created_at = now
        note.updated_at = now
        relative_dir = resolve_note_directory(
            note.archived,
            str(get_setting("notes_directory", "Notes") or "Notes"),
            str(get_setting("notes_archive_directory", "Notes/Archive") or "Notes/Archive"),
        )
        target_dir = safe_join(root, relative_dir)
        if target_dir is None:
            raise ValueError("notes directory must stay inside the vault")
        target_dir.mkdir(parents=True, exist_ok=True)
        current = self._path_for(note.id, note.owner)
        target = current if current and current.parent == target_dir else target_dir / note_filename(note.title, note.id)
        if target.exists() and (current is None or target.resolve() != current.resolve()):
            target = target_dir / f"{target.stem}-{note.id[:8]}.md"
        payload = note_to_markdown(note)
        fd, temp_name = tempfile.mkstemp(prefix=".note-", suffix=".tmp", dir=str(target_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
            if current and current.resolve() != target.resolve() and current.exists():
                current.unlink()
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return note

    def delete(self, note_id: str, owner: Optional[str] = None) -> bool:
        note = self.find(note_id, owner)
        if note is None:
            return False
        path = self._path_for(note.id, owner)
        if path is None:
            return False
        path.unlink()
        return True


STORE = MarkdownNotesStore()
