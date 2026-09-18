import os

import pytest

import src.rag_sensitivity as sensitivity
from src.notes_markdown import NoteRecord, markdown_to_note
from src.notes_store import MarkdownNotesStore
from src.rag_sensitivity import VaultReadOnlyError


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


def test_readonly_notes_can_be_read_but_not_edited_or_deleted(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(sensitivity, "vault_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        sensitivity,
        "_safe_folder_policy_map",
        lambda: ({"Notes": sensitivity.FolderPolicy(readonly=True)}, True),
    )
    notes = tmp_path / "Notes"
    notes.mkdir()
    path = notes / "locked.md"
    path.write_text(
        "---\nid: locked\ntitle: Locked\nowner: alice\n---\noriginal", encoding="utf-8"
    )
    store = MarkdownNotesStore()

    loaded = store.find("locked", "alice")
    assert loaded is not None and loaded.content == "original"
    loaded.content = "changed"
    with pytest.raises(VaultReadOnlyError):
        store.save(loaded)
    with pytest.raises(VaultReadOnlyError):
        store.delete("locked", "alice")
    assert path.read_text(encoding="utf-8").endswith("original")


def test_human_override_can_edit_and_delete_llm_readonly_note(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(sensitivity, "vault_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        sensitivity,
        "_safe_folder_policy_map",
        lambda: ({"Notes": sensitivity.FolderPolicy(readonly=True)}, True),
    )
    notes = tmp_path / "Notes"
    notes.mkdir()
    path = notes / "human.md"
    path.write_text(
        "---\nid: human\ntitle: Human\nowner: alice\n---\noriginal", encoding="utf-8"
    )
    store = MarkdownNotesStore()
    note = store.find("human", "alice")
    note.content = "changed by human"

    store.save(note, enforce_readonly=False)
    assert "changed by human" in path.read_text(encoding="utf-8")
    assert store.delete("human", "alice", enforce_readonly=False) is True
    assert not path.exists()


def test_archiving_checks_both_source_and_destination_policy(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(sensitivity, "vault_root", lambda: str(tmp_path))
    notes = tmp_path / "Notes"
    notes.mkdir()
    path = notes / "move.md"
    path.write_text(
        "---\nid: move\ntitle: Move\nowner: alice\n---\ncontent", encoding="utf-8"
    )
    monkeypatch.setattr(
        sensitivity,
        "_safe_folder_policy_map",
        lambda: ({"Notes/Archive": sensitivity.FolderPolicy(readonly=True)}, True),
    )
    store = MarkdownNotesStore()
    note = store.find("move", "alice")
    assert note is not None
    note.archived = True

    with pytest.raises(VaultReadOnlyError):
        store.save(note)
    assert path.exists()
