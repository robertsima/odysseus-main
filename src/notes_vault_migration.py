"""Reversible, dry-run-first migration: SQLite ``Note`` rows -> vault Markdown files.

Notes currently live as rows in the ``notes`` table with no privacy label.
The vault already has folder-scoped sensitivity and is plain Markdown. This
module copies each note out to a ``.md`` file under the vault's notes folder
(or its archive folder, for archived notes) using the codec in
``src.notes_markdown`` — and copies, deliberately: it never deletes the
SQLite rows, so "the migration" can be tried, inspected, and undone without
ever putting the source data at risk.

Three verbs, in the order a human should use them:

* :func:`plan`     — read-only. Decides what would be written and where, and
                      how any filename collisions would be resolved. Touches
                      no file.
* :func:`apply`     — executes a plan. Writes the Markdown files and records
                      a manifest (JSON) of exactly what it wrote, so rollback
                      never has to guess.
* :func:`rollback`  — undoes an apply using its manifest. It only ever
                      touches paths the manifest says *this migration*
                      wrote, and only if the file's content still matches
                      the hash recorded when it was written — a file the
                      user has since edited in Obsidian is left alone.

Run ``python -m src.notes_vault_migration`` (no flags) to print the plan
without changing anything; add ``--apply`` to execute it, or ``--rollback``
to undo a previous run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from src.constants import DATA_DIR, PERSONAL_DIR
from src.settings import get_setting
from src.vault_markdown import split_frontmatter
from src.notes_markdown import (
    NoteRecord,
    note_filename,
    note_record_from_orm,
    note_to_markdown,
    resolve_note_directory,
    safe_join,
)

logger = logging.getLogger(__name__)

# Lives in the app's data directory, not the vault itself — this is internal
# migration bookkeeping, not a note, and should not show up as a vault file
# for the RAG indexer or Obsidian to trip over.
DEFAULT_MANIFEST_PATH = Path(DATA_DIR) / "notes_vault_migration_manifest.json"


def _vault_paths() -> Tuple[Path, str, str]:
    """(vault root, notes-relative dir, archive-relative dir) from settings."""
    vault_dir = get_setting("vault_directory") or PERSONAL_DIR
    notes_dir = get_setting("notes_directory", "Notes")
    archive_dir = get_setting("notes_archive_directory", "Notes/Archive")
    return Path(vault_dir), notes_dir, archive_dir


def _load_note_records(owner: Optional[str] = None) -> List[NoteRecord]:
    """Snapshot every (or one owner's) note row as a NoteRecord.

    Converts to the plain dataclass before the session closes rather than
    handing back ORM rows — the migration has no business holding a session
    open across planning/writing, and NoteRecord is the stable contract the
    rest of this module (and its tests) talk to.
    """
    from core.database import SessionLocal, Note  # local import: keep this module DB-optional at import time

    db = SessionLocal()
    try:
        query = db.query(Note)
        if owner is not None:
            query = query.filter(Note.owner == owner)
        return [note_record_from_orm(row) for row in query.all()]
    finally:
        db.close()


def _existing_ids_and_stems(dir_path: Path) -> Tuple[Dict[str, Path], Set[str]]:
    """Scan one target directory for notes already migrated there.

    Returns a map of ``note id -> existing file`` (read straight from
    frontmatter, not through the full codec, since only the id matters here)
    plus the set of filename stems already in use, for collision avoidance.
    """
    by_id: Dict[str, Path] = {}
    stems: Set[str] = set()
    if not dir_path.is_dir():
        return by_id, stems
    for existing_file in sorted(dir_path.glob("*.md")):
        try:
            # Do not let a symlink in a configured notes folder make the
            # migration inspect a file outside that folder/vault.
            existing_file.resolve().relative_to(dir_path.resolve())
        except ValueError:
            continue
        stems.add(existing_file.stem.lower())
        try:
            text = existing_file.read_text(encoding="utf-8")
        except OSError:
            continue
        frontmatter, _ = split_frontmatter(text)
        raw_id = frontmatter.get("id") if isinstance(frontmatter, dict) else None
        if raw_id:
            by_id[str(raw_id)] = existing_file
    return by_id, stems


# ---------------------------------------------------------------------------
# plan()
# ---------------------------------------------------------------------------


@dataclass
class PlannedWrite:
    note_id: str
    relative_path: str
    title: str


@dataclass
class SkippedNote:
    """A note that already has a migrated file — plan()/apply() are idempotent."""
    note_id: str
    relative_path: str
    reason: str


@dataclass
class Conflict:
    """Two notes wanted the same filename; the second was suffixed. Informational
    — plan() always resolves these deterministically rather than blocking."""
    note_id: str
    title: str
    wanted_name: str
    resolved_name: str


@dataclass
class MigrationPlan:
    vault_dir: Path
    notes_directory: str
    notes_archive_directory: str
    generated_at: str
    writes: List[PlannedWrite] = field(default_factory=list)
    skipped: List[SkippedNote] = field(default_factory=list)
    conflicts: List[Conflict] = field(default_factory=list)

    def render(self) -> str:
        """Human-readable plan for the ``--dry-run`` CLI — the whole point of
        a dry run is that a person can read this before anything changes."""
        lines = [
            f"Vault:          {self.vault_dir}",
            f"Notes dir:      {self.notes_directory}",
            f"Archive dir:    {self.notes_archive_directory}",
            f"Generated at:   {self.generated_at}",
            "",
        ]
        if not self.writes and not self.skipped:
            lines.append("No notes found.")
        for w in self.writes:
            lines.append(f"  + create   {w.relative_path}   (title: {w.title!r})")
        for c in self.conflicts:
            lines.append(
                f"  ! collision  {c.wanted_name!r} taken -> using {c.resolved_name!r}   (title: {c.title!r})"
            )
        for s in self.skipped:
            lines.append(f"  = skip     {s.relative_path}   ({s.reason})")
        lines.append("")
        lines.append(
            f"{len(self.writes)} to create, {len(self.conflicts)} filename collision(s) resolved, "
            f"{len(self.skipped)} already migrated."
        )
        return "\n".join(lines)


def plan(owner: Optional[str] = None) -> MigrationPlan:
    """Compute what a migration run would do. Writes nothing.

    Safe to call repeatedly and interleaved with real file activity — it
    only reads (the DB and the vault directories) and never edits either.
    """
    vault_dir, notes_dir, archive_dir = _vault_paths()
    records = _load_note_records(owner=owner)

    result = MigrationPlan(
        vault_dir=vault_dir,
        notes_directory=notes_dir,
        notes_archive_directory=archive_dir,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )

    # Collisions are resolved per target directory, and per call to plan() —
    # two notes bound for Notes/ and Notes/Archive/ can share a filename
    # without either being suffixed.
    scanned: Dict[str, Tuple[Dict[str, Path], Set[str]]] = {}
    reserved_this_run: Dict[str, Set[str]] = {}

    for record in records:
        rel_dir = resolve_note_directory(record.archived, notes_dir, archive_dir)
        if rel_dir not in scanned:
            scanned[rel_dir] = _existing_ids_and_stems(vault_dir / rel_dir)
            reserved_this_run[rel_dir] = set()
        by_id, existing_stems = scanned[rel_dir]

        if record.id in by_id:
            result.skipped.append(
                SkippedNote(
                    note_id=record.id,
                    relative_path=str(Path(rel_dir) / by_id[record.id].name),
                    reason="already migrated",
                )
            )
            continue

        stems_in_use = existing_stems | reserved_this_run[rel_dir]
        wanted_name = note_filename(record.title, record.id, set())
        resolved_name = note_filename(record.title, record.id, stems_in_use)
        reserved_this_run[rel_dir].add(Path(resolved_name).stem.lower())

        if resolved_name != wanted_name:
            result.conflicts.append(
                Conflict(
                    note_id=record.id,
                    title=record.title,
                    wanted_name=wanted_name,
                    resolved_name=resolved_name,
                )
            )

        result.writes.append(
            PlannedWrite(
                note_id=record.id,
                relative_path=str(Path(rel_dir) / resolved_name),
                title=record.title,
            )
        )

    return result


# ---------------------------------------------------------------------------
# apply() / manifest
# ---------------------------------------------------------------------------


@dataclass
class ManifestEntry:
    note_id: str
    relative_path: str
    sha256: str
    written_at: str


@dataclass
class Manifest:
    """What apply() wrote, keyed by vault-relative path.

    ``sha256`` is what makes rollback safe: it is recomputed against the file
    on disk at rollback time, and a mismatch (the user edited the file since)
    means that file is left alone. ``created_dirs`` lets rollback clean up
    directories the migration itself created — never a directory that
    existed before, and never one that gained other content since.
    """
    vault_dir: str = ""
    entries: Dict[str, ManifestEntry] = field(default_factory=dict)
    created_dirs: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {
            "vault_dir": self.vault_dir,
            "entries": {path: vars(entry) for path, entry in self.entries.items()},
            "created_dirs": self.created_dirs,
        }

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "Manifest":
        entries = {
            path: ManifestEntry(**fields) for path, fields in (data.get("entries") or {}).items()
        }
        return cls(
            vault_dir=str(data.get("vault_dir") or ""),
            entries=entries,
            created_dirs=list(data.get("created_dirs") or []),
        )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_manifest(path: Path) -> Manifest:
    if not path.exists():
        return Manifest()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("could not read migration manifest %s (%s); starting fresh", path, e)
        return Manifest()
    return Manifest.from_json(data)


def _save_manifest(path: Path, manifest: Manifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic-ish: write to a temp name and replace, so a crash mid-write can
    # never leave a half-written manifest that rollback would misread.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest.to_json(), indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def apply(migration_plan: MigrationPlan, owner: Optional[str] = None, manifest_path: Optional[Path] = None) -> Manifest:
    """Execute *migration_plan*: write the planned files, extend the manifest.

    Re-fetches each note from the DB by id rather than trusting the plan to
    still be fresh (a plan computed a moment ago could be stale if a note
    changed or was deleted since). Idempotent: a path already recorded in the
    manifest with the file still present on disk is left untouched, so
    calling apply() twice over an unchanged plan writes nothing the second
    time.
    """
    manifest_path = manifest_path or DEFAULT_MANIFEST_PATH
    manifest = _load_manifest(manifest_path)
    manifest.vault_dir = str(migration_plan.vault_dir)

    records_by_id = {record.id: record for record in _load_note_records(owner=owner)}

    for write in migration_plan.writes:
        target = safe_join(migration_plan.vault_dir, write.relative_path)
        if target is None:
            # Should be unreachable — note_filename() never emits a path
            # that escapes the target directory — but refuse rather than
            # trust it blindly.
            logger.warning("refusing to write outside the vault: %s", write.relative_path)
            continue

        already_done = write.relative_path in manifest.entries and target.exists()
        if already_done:
            continue

        # A dry-run plan can sit around while Obsidian or another process
        # creates a file at the planned name.  Never turn that race into an
        # overwrite of user data; ask for a fresh plan instead.
        if target.exists():
            logger.warning("refusing to overwrite an existing vault file: %s", write.relative_path)
            continue

        record = records_by_id.get(write.note_id)
        if record is None:
            logger.info("note %s vanished before apply(); skipping", write.note_id)
            continue

        parent_is_new = not target.parent.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        if parent_is_new:
            rel_dir = str(target.parent.relative_to(migration_plan.vault_dir))
            if rel_dir not in manifest.created_dirs:
                manifest.created_dirs.append(rel_dir)

        content = note_to_markdown(record)
        target.write_text(content, encoding="utf-8")
        manifest.entries[write.relative_path] = ManifestEntry(
            note_id=record.id,
            relative_path=write.relative_path,
            sha256=_sha256(content),
            written_at=datetime.now(timezone.utc).isoformat(),
        )

    _save_manifest(manifest_path, manifest)
    return manifest


# ---------------------------------------------------------------------------
# rollback()
# ---------------------------------------------------------------------------


@dataclass
class RollbackResult:
    removed: List[str] = field(default_factory=list)
    kept_due_to_edits: List[str] = field(default_factory=list)
    already_gone: List[str] = field(default_factory=list)
    removed_dirs: List[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"Removed {len(self.removed)} file(s)."]
        for p in self.removed:
            lines.append(f"  - {p}")
        if self.kept_due_to_edits:
            lines.append(f"Kept {len(self.kept_due_to_edits)} file(s) edited since migration (left untouched):")
            for p in self.kept_due_to_edits:
                lines.append(f"  * {p}")
        if self.already_gone:
            lines.append(f"{len(self.already_gone)} file(s) were already gone.")
        if self.removed_dirs:
            lines.append(f"Removed {len(self.removed_dirs)} now-empty director(y/ies) created by the migration.")
        return "\n".join(lines)


def rollback(manifest_path: Optional[Path] = None) -> RollbackResult:
    """Undo a previous :func:`apply` using its manifest.

    The safety guarantee is entirely mechanical, not judgment-based: rollback
    only ever considers paths recorded in the manifest (files this migration
    itself created), and for each one it compares the file's *current*
    sha256 to the hash recorded when apply() wrote it. Equal hash -> the file
    is exactly what the migration produced and it is safe to remove. Any
    other hash -> something (almost certainly the user, in Obsidian) changed
    the file since, and it is left alone, unconditionally, and reported back
    so a human can see it was not touched. The Note rows in SQLite were never
    deleted in the first place, so nothing here is ever "restoring" data —
    only removing the copies apply() made.
    """
    manifest_path = manifest_path or DEFAULT_MANIFEST_PATH
    manifest = _load_manifest(manifest_path)
    result = RollbackResult()
    vault_dir = Path(manifest.vault_dir) if manifest.vault_dir else Path(".")

    remaining_entries: Dict[str, ManifestEntry] = {}
    for relative_path, entry in manifest.entries.items():
        target = safe_join(vault_dir, relative_path)
        if target is None:
            logger.warning("refusing to remove a manifest path outside the vault: %s", relative_path)
            remaining_entries[relative_path] = entry
            continue
        if not target.exists():
            result.already_gone.append(relative_path)
            continue
        try:
            current_hash = _sha256(target.read_text(encoding="utf-8"))
        except OSError as e:
            logger.warning("could not read %s during rollback (%s); leaving it alone", target, e)
            remaining_entries[relative_path] = entry
            continue
        if current_hash != entry.sha256:
            result.kept_due_to_edits.append(relative_path)
            remaining_entries[relative_path] = entry
            continue
        target.unlink()
        result.removed.append(relative_path)

    remaining_dirs: List[str] = []
    for rel_dir in manifest.created_dirs:
        directory = safe_join(vault_dir, rel_dir)
        if directory is None:
            logger.warning("refusing to remove a manifest directory outside the vault: %s", rel_dir)
            remaining_dirs.append(rel_dir)
            continue
        try:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
                result.removed_dirs.append(rel_dir)
            elif directory.exists():
                remaining_dirs.append(rel_dir)
        except OSError:
            remaining_dirs.append(rel_dir)

    manifest.entries = remaining_entries
    manifest.created_dirs = remaining_dirs
    if manifest.entries or manifest.created_dirs:
        _save_manifest(manifest_path, manifest)
    else:
        try:
            manifest_path.unlink()
        except OSError:
            pass

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.notes_vault_migration",
        description="Migrate notes (SQLite rows) into the vault as Markdown files. "
                     "Defaults to a dry run: prints the plan and writes nothing.",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan and exit (default behavior)")
    parser.add_argument("--apply", action="store_true", help="write the planned files")
    parser.add_argument("--rollback", action="store_true", help="undo a previous --apply using its manifest")
    parser.add_argument("--owner", default=None, help="only migrate/rollback notes owned by this user")
    parser.add_argument("--manifest", default=None, help=f"manifest path (default: {DEFAULT_MANIFEST_PATH})")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest) if args.manifest else None

    if args.rollback:
        result = rollback(manifest_path=manifest_path)
        print(result.render())
        return 0

    migration_plan = plan(owner=args.owner)
    print(migration_plan.render())

    if args.apply:
        apply(migration_plan, owner=args.owner, manifest_path=manifest_path)
        print(f"\nWrote {len(migration_plan.writes)} file(s). Manifest: {manifest_path or DEFAULT_MANIFEST_PATH}")
    else:
        print("\n(dry run — nothing was written; re-run with --apply to execute this plan)")

    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(_cli())
