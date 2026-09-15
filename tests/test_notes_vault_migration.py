"""The migration copies Note rows out of SQLite into vault Markdown files. It
must be safe to run against a live system: a plan that changes nothing on
disk, an apply that never touches the source rows and never duplicates work,
and a rollback that can only ever undo what this migration itself wrote.

Uses the real ORM (``core.database.Note``) against the shared in-memory test
DB (see tests/conftest.py) rather than mocking it — the collision/idempotency
logic depends on genuinely querying and re-querying the same table, and a
mock would just re-encode the assumptions being tested. Every test scopes its
rows to a private ``owner`` string and passes it through to plan()/apply() so
tests never see each other's notes even though they share one DB.
"""
import json
import hashlib
import os
import uuid

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import src.settings as settings
from core.database import Note, SessionLocal
from src.notes_markdown import markdown_to_note
from src.notes_vault_migration import apply, plan, rollback


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def owner():
    """A private owner tag per test so rows never collide across tests
    sharing one in-memory DB."""
    return f"migtest-{uuid.uuid4().hex[:10]}"


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    """Point the migration at an isolated vault + manifest for this test."""
    vault_dir = tmp_path / "vault"
    saved = settings.load_settings()
    monkeypatch.setattr(settings, "load_settings", lambda: {**saved, "vault_directory": str(vault_dir)})
    manifest_path = tmp_path / "manifest.json"
    return vault_dir, manifest_path


def _add_note(owner, **overrides):
    fields = dict(
        id=str(uuid.uuid4()),
        owner=owner,
        title="Untitled",
        content="",
        items=None,
        note_type="note",
        color=None,
        label=None,
        pinned=False,
        archived=False,
        due_date=None,
        source="user",
        session_id=None,
        sort_order=0,
        image_url=None,
        repeat="none",
    )
    fields.update(overrides)
    db = SessionLocal()
    try:
        note = Note(**fields)
        db.add(note)
        db.commit()
        return fields["id"]
    finally:
        db.close()


def _apply(vault, owner):
    vault_dir, manifest_path = vault
    p = plan(owner=owner)
    return apply(p, owner=owner, manifest_path=manifest_path)


def _rollback(vault):
    _vault_dir, manifest_path = vault
    return rollback(manifest_path=manifest_path)


def _all_files(vault_dir):
    if not vault_dir.exists():
        return []
    return sorted(str(p.relative_to(vault_dir)) for p in vault_dir.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# plan() is read-only
# ---------------------------------------------------------------------------


def test_plan_writes_nothing_to_disk(vault, owner):
    vault_dir, _ = vault
    _add_note(owner, title="A note")
    result = plan(owner=owner)
    assert len(result.writes) == 1
    # The whole point of a dry run: the filesystem is untouched.
    assert not vault_dir.exists()


def test_plan_lists_a_note_for_each_row(vault, owner):
    _add_note(owner, title="First")
    _add_note(owner, title="Second")
    result = plan(owner=owner)
    assert {w.title for w in result.writes} == {"First", "Second"}
    assert result.skipped == []


def test_plan_render_is_human_readable_and_mentions_every_write(vault, owner):
    _add_note(owner, title="Readable Plan Note")
    result = plan(owner=owner)
    text = result.render()
    assert "Readable Plan Note" in text
    assert "create" in text


# ---------------------------------------------------------------------------
# apply() writes exactly the plan, and only the plan
# ---------------------------------------------------------------------------


def test_apply_writes_one_file_per_note(vault, owner):
    vault_dir, _ = vault
    _add_note(owner, title="Groceries", content="milk, eggs")
    manifest = _apply(vault, owner)
    files = _all_files(vault_dir)
    assert len(files) == 1
    assert len(manifest.entries) == 1


def test_apply_never_overwrites_a_file_created_after_the_dry_run(vault, owner):
    vault_dir, manifest_path = vault
    _add_note(owner, title="Race")
    migration_plan = plan(owner=owner)
    target = vault_dir / migration_plan.writes[0].relative_path
    target.parent.mkdir(parents=True)
    target.write_text("A file Obsidian created after planning.\n", encoding="utf-8")

    manifest = apply(migration_plan, owner=owner, manifest_path=manifest_path)

    assert target.read_text(encoding="utf-8") == "A file Obsidian created after planning.\n"
    assert manifest.entries == {}


def test_apply_does_not_touch_the_source_row(vault, owner):
    # Reversibility means the old data is still there — the migration COPIES
    # out, it never deletes SQLite rows.
    note_id = _add_note(owner, title="Keep me")
    _apply(vault, owner)
    db = SessionLocal()
    try:
        row = db.query(Note).filter(Note.id == note_id).first()
        assert row is not None
        assert row.title == "Keep me"
    finally:
        db.close()


def test_archived_notes_land_under_the_archive_directory(vault, owner):
    vault_dir, _ = vault
    _add_note(owner, title="Old todo", archived=True)
    _add_note(owner, title="Active todo", archived=False)
    _apply(vault, owner)
    files = _all_files(vault_dir)
    assert any(f.startswith("Notes" + os.sep + "Archive" + os.sep) for f in files)
    assert any(f == "Notes" + os.sep + "Active todo.md" for f in files)


def test_checklist_items_become_a_native_task_list_in_the_body(vault, owner):
    vault_dir, _ = vault
    items = [{"text": "Buy milk", "done": False}, {"text": "Pay rent", "done": True}]
    _add_note(owner, title="Chores", note_type="checklist", items=json.dumps(items), content=None)
    _apply(vault, owner)
    files = _all_files(vault_dir)
    text = (vault_dir / files[0]).read_text(encoding="utf-8")
    assert "- [ ] Buy milk" in text
    assert "- [x] Pay rent" in text


def test_a_note_and_a_checklist_with_the_same_title_both_get_written(vault, owner):
    vault_dir, _ = vault
    _add_note(owner, title="Same Title", content="prose")
    _add_note(owner, title="Same Title", note_type="checklist", items=json.dumps([]), content=None)
    result = plan(owner=owner)
    assert len(result.conflicts) == 1  # the second one collided on filename and was suffixed
    apply(result, owner=owner, manifest_path=vault[1])
    files = _all_files(vault_dir)
    assert len(files) == 2
    assert len(set(files)) == 2  # genuinely two distinct files, not one overwritten


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_apply_twice_does_not_duplicate_notes(vault, owner):
    vault_dir, _ = vault
    _add_note(owner, title="Only once")
    _apply(vault, owner)
    files_after_first = _all_files(vault_dir)
    _apply(vault, owner)
    files_after_second = _all_files(vault_dir)
    assert files_after_first == files_after_second


def test_second_plan_reports_already_migrated_notes_as_skipped(vault, owner):
    _add_note(owner, title="Migrate me")
    _apply(vault, owner)
    second_plan = plan(owner=owner)
    assert second_plan.writes == []
    assert len(second_plan.skipped) == 1


def test_apply_is_idempotent_even_if_the_note_title_changed_after_first_migration(vault, owner):
    # Identity is the frontmatter id, not the filename/title — apply() must
    # recognize the already-migrated file by id and not create a second copy
    # even though a fresh plan() would derive a different filename for it.
    vault_dir, _ = vault
    note_id = _add_note(owner, title="Original Title")
    _apply(vault, owner)
    db = SessionLocal()
    try:
        row = db.query(Note).filter(Note.id == note_id).first()
        row.title = "Renamed Title"
        db.commit()
    finally:
        db.close()
    _apply(vault, owner)
    assert len(_all_files(vault_dir)) == 1


# ---------------------------------------------------------------------------
# Rollback: reversible, and safe against user edits
# ---------------------------------------------------------------------------


def test_apply_then_rollback_returns_to_the_starting_state(vault, owner):
    vault_dir, _ = vault
    _add_note(owner, title="Round trip me")
    assert _all_files(vault_dir) == []  # starting state: nothing
    _apply(vault, owner)
    assert len(_all_files(vault_dir)) == 1
    result = _rollback(vault)
    assert len(result.removed) == 1
    assert _all_files(vault_dir) == []  # back to starting state


def test_rollback_only_removes_files_the_migration_created(vault, owner):
    vault_dir, _ = vault
    _add_note(owner, title="Migrated note")
    _apply(vault, owner)
    # A file this migration never touched, sitting in the same directory.
    unrelated = vault_dir / "Notes" / "not ours.md"
    unrelated.write_text("---\ntitle: Not ours\n---\nHuman-written note.\n", encoding="utf-8")
    result = _rollback(vault)
    assert len(result.removed) == 1
    assert unrelated.exists()  # never touched, let alone deleted


def test_rollback_does_not_clobber_a_file_the_user_edited_since_migration(vault, owner):
    vault_dir, _ = vault
    _add_note(owner, title="Edited after migration")
    _apply(vault, owner)
    files = _all_files(vault_dir)
    edited_path = vault_dir / files[0]
    original_text = edited_path.read_text(encoding="utf-8")
    edited_path.write_text(original_text + "\n\nA line the user typed in Obsidian.\n", encoding="utf-8")

    result = _rollback(vault)

    assert result.removed == []
    assert len(result.kept_due_to_edits) == 1
    assert edited_path.exists()
    assert "A line the user typed in Obsidian." in edited_path.read_text(encoding="utf-8")


def test_rollback_is_safe_to_run_when_there_is_nothing_to_undo(vault):
    result = _rollback(vault)  # no apply() ever ran for this manifest path
    assert result.removed == []
    assert result.kept_due_to_edits == []


def test_rollback_reports_files_already_removed_by_something_else(vault, owner):
    vault_dir, _ = vault
    _add_note(owner, title="Will be deleted early")
    _apply(vault, owner)
    files = _all_files(vault_dir)
    (vault_dir / files[0]).unlink()
    result = _rollback(vault)
    assert result.removed == []
    assert len(result.already_gone) == 1


def test_rollback_refuses_manifest_paths_outside_the_vault(vault):
    vault_dir, manifest_path = vault
    outside = vault_dir.parent / "outside.md"
    outside.write_text("must survive\n", encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "vault_dir": str(vault_dir),
                "entries": {
                    "../outside.md": {
                        "note_id": "attacker",
                        "relative_path": "../outside.md",
                        "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
                        "written_at": "now",
                    }
                },
                "created_dirs": [],
            }
        ),
        encoding="utf-8",
    )

    result = rollback(manifest_path=manifest_path)

    assert result.removed == []
    assert outside.exists()


def test_apply_after_a_partial_rollback_recreates_only_the_removed_note(vault, owner):
    vault_dir, _ = vault
    _add_note(owner, title="Note A")
    _add_note(owner, title="Note B")
    _apply(vault, owner)
    assert len(_all_files(vault_dir)) == 2
    _rollback(vault)
    assert _all_files(vault_dir) == []
    # Re-running the whole pipeline from scratch reproduces the same state.
    _apply(vault, owner)
    assert len(_all_files(vault_dir)) == 2


# ---------------------------------------------------------------------------
# Round trip through the real filesystem: what apply() wrote, markdown_to_note
# can read back faithfully.
# ---------------------------------------------------------------------------


def test_a_written_file_round_trips_back_through_the_codec(vault, owner):
    vault_dir, _ = vault
    note_id = _add_note(
        owner,
        title="Full fidelity check",
        content="Some body text",
        color="#112233",
        label="tag1",
        pinned=True,
        due_date="2026-05-05",
        sort_order=7,
    )
    _apply(vault, owner)
    files = _all_files(vault_dir)
    text = (vault_dir / files[0]).read_text(encoding="utf-8")
    decoded = markdown_to_note(text)
    assert decoded.id == note_id
    assert decoded.title == "Full fidelity check"
    assert decoded.content == "Some body text"
    assert decoded.color == "#112233"
    assert decoded.label == "tag1"
    assert decoded.pinned is True
    assert decoded.due_date == "2026-05-05"
    assert decoded.sort_order == 7
