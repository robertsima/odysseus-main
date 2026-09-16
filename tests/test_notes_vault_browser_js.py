from pathlib import Path


SRC = (Path(__file__).resolve().parent.parent / "static" / "js" / "notes.js").read_text(
    encoding="utf-8"
)
STYLE = (Path(__file__).resolve().parent.parent / "static" / "style.css").read_text(
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


def test_vault_folders_are_collapsed_until_the_user_opens_them():
    tree_renderer = SRC.split("function _vaultTreeHtml", 1)[1].split("function _vaultSearchHtml", 1)[0]
    assert 'class="vault-tree-folder" open' not in tree_renderer
    assert "_vaultExpandedFolders.has(folderPath)" in tree_renderer
    assert "folder.addEventListener('toggle'" in SRC
    assert "_vaultExpandedFolders.clear()" in SRC


def test_vault_search_is_flat_and_does_not_auto_expand_folders():
    assert "function _vaultSearchHtml(root)" in SRC
    assert "_searchQuery ? _vaultSearchHtml(_vaultTree) : _vaultTreeHtml(_vaultTree)" in SRC
    search_renderer = SRC.split("function _vaultSearchHtml", 1)[1].split("function _renderVault", 1)[0]
    assert "matches.map(node => _vaultFileHtml(node, 0, true))" in search_renderer


def test_vault_explorer_uses_theme_semantic_colors_and_visible_focus():
    vault_css = STYLE.split(".vault-browser {", 1)[1].split(".note-card.note-card-sliding-out", 1)[0]
    assert "--vault-public: var(--color-success" in vault_css
    assert "--vault-private: var(--hl-keyword" in vault_css
    assert "--vault-readonly: var(--color-warning" in vault_css
    assert ".vault-tree-file:focus-visible" in vault_css
    for hardcoded in ("#68b984", "#d782e8", "#e6b25d"):
        assert hardcoded not in vault_css
