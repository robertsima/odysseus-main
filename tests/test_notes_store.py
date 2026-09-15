import os

import pytest

from src.notes_markdown import NoteRecord, markdown_to_note
from src.notes_store import MarkdownNotesStore


def _configure(monkeypatch, tmp_path):
    import src.notes_store as module

    values = {
        "notes_directory": "Notes",
        "notes_archive_directory": "Notes/Archive",
    }
    monkeypatch.setattr(module, "vault_root", lambda: str(tmp_path))
    monkeypatch.setattr(module, "get_setting", lambda key, default=None: values.get(key, default))


def test_store_round_trip_moves_archives_and_preserves_user_frontmatter(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    store = MarkdownNotesStore()
    note = NoteRecord(
        id="note-1234", owner="alice", title="Private plan", content="draft",
        extra_frontmatter={"sensitivity": "private", "aliases": ["plan"]},
    )

    store.save(note)
    note.content = "edited"
    note.archived = True
    store.save(note)

    assert not list((tmp_path / "Notes").glob("*.md"))
    files = list((tmp_path / "Notes" / "Archive").glob("*.md"))
    assert len(files) == 1
    loaded = markdown_to_note(files[0].read_text(encoding="utf-8"))
    assert loaded.content == "edited"
    assert loaded.extra_frontmatter == {"sensitivity": "private", "aliases": ["plan"]}


def test_hand_authored_markdown_without_id_has_stable_identity(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    notes = tmp_path / "Notes"
    notes.mkdir()
    path = notes / "manual.markdown"
    path.write_text("---\ntitle: Manual\nsensitivity: private\n---\nhello", encoding="utf-8")
    store = MarkdownNotesStore()

    first = store.list(None)[0]
    second = store.list(None)[0]

    assert first.id == second.id
    assert first.content == "hello"
    assert first.extra_frontmatter["sensitivity"] == "private"


def test_store_does_not_read_markdown_symlinked_outside_the_vault(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    outside = tmp_path.parent / "outside-note.md"
    outside.write_text("---\nid: outside\ntitle: Secret\nowner: alice\n---\nsecret", encoding="utf-8")
    notes = tmp_path / "Notes"
    notes.mkdir()
    link = notes / "linked.md"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    assert MarkdownNotesStore().list("alice") == []


def test_delete_uses_the_requested_owner_when_duplicate_ids_exist(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    notes = tmp_path / "Notes"
    notes.mkdir()
    (notes / "alice.md").write_text(
        "---\nid: duplicate\ntitle: Alice\nowner: alice\n---\na", encoding="utf-8"
    )
    (notes / "bob.md").write_text(
        "---\nid: duplicate\ntitle: Bob\nowner: bob\n---\nb", encoding="utf-8"
    )

    store = MarkdownNotesStore()
    assert store.delete("duplicate", "bob") is True
    assert (notes / "alice.md").exists()
    assert not (notes / "bob.md").exists()
