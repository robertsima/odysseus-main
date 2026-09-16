from pathlib import Path


SRC = (Path(__file__).resolve().parent.parent / "static" / "js" / "notes.js").read_text(
    encoding="utf-8"
)


def test_notes_panel_exposes_vault_tree_and_editor():
    assert 'data-notes-mode="vault"' in SRC
    assert "/api/personal/vault/tree" in SRC
    assert "/api/personal/vault/file" in SRC
    assert 'id="vault-file-editor"' in SRC


def test_vault_editor_shows_independent_llm_policy_badges():
    assert "function _vaultPolicyLabels(file)" in SRC
    assert "[sensitivity, 'readonly']" in SRC
    assert "applies to LLMs and agents, not your UI access" in SRC


def test_failed_save_preserves_draft_and_external_mtime_is_sent():
    assert "modified: _vaultFile.modified ?? null" in SRC
    assert "Keep the failed draft in the textarea" in SRC
    assert "if (_vaultDirty) return" in SRC
