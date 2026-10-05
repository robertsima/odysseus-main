import json

from src.personal_docs import PersonalDocsManager


def test_manager_ignores_invalid_persisted_state_shapes(tmp_path):
    (tmp_path / "indexed_directories.json").write_text(json.dumps({"bad": "shape"}))
    (tmp_path / "excluded_files.json").write_text(json.dumps({"bad": "shape"}))

    manager = PersonalDocsManager(str(tmp_path))

    assert manager.indexed_directories == []
    assert manager.excluded_files == set()


def test_manager_filters_invalid_persisted_state_rows(tmp_path):
    (tmp_path / "indexed_directories.json").write_text(json.dumps(["/tmp/docs", 123]))
    (tmp_path / "excluded_files.json").write_text(json.dumps(["/tmp/docs/a.txt", None]))

    manager = PersonalDocsManager(str(tmp_path))

    assert manager.indexed_directories == ["/tmp/docs"]
    assert manager.excluded_files == {"/tmp/docs/a.txt"}


def test_configured_vault_root_can_keep_manager_state_elsewhere(tmp_path):
    vault = tmp_path / "Vault"
    state = tmp_path / "app-state"
    vault.mkdir()
    (vault / "Notes").mkdir()
    note = vault / "Notes" / "existing.md"
    note.write_text("---\nid: n1\ntitle: Existing\n---\nbody", encoding="utf-8")

    manager = PersonalDocsManager(str(vault), state_dir=str(state))

    assert any(row["path"] == str(note) for row in manager.index)
    assert manager.directories_file == str(state / "indexed_directories.json")
    assert state.exists()
    assert not (vault / "indexed_directories.json").exists()
