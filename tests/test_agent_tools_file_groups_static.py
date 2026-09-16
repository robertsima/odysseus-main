from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ADMIN = (ROOT / "static/js/admin.js").read_text(encoding="utf-8")
STYLE = (ROOT / "static/style.css").read_text(encoding="utf-8")


def test_personal_document_files_are_grouped_by_directory():
    assert "function _ragFileGroups(files)" in ADMIN
    assert "function _renderRagFileGroups(files)" in ADMIN
    assert "displayPath.lastIndexOf('/')" in ADMIN
    assert "fileList.innerHTML = _renderRagFileGroups(files)" in ADMIN


def test_file_groups_start_collapsed_and_keep_delete_paths():
    renderer = ADMIN.split("function _renderRagFileGroups", 1)[1].split("async function loadRag", 1)[0]
    assert '<details class="admin-rag-file-group">' in renderer
    assert '<details class="admin-rag-file-group" open>' not in renderer
    assert 'data-adm-rag-file="${esc(file.path || file.name)}"' in renderer


def test_grouped_file_settings_are_compact_and_theme_native():
    assert ".admin-rag-file-group" in STYLE
    assert ".admin-rag-file-group[open] > summary::before" in STYLE
    assert "background: color-mix(in srgb, var(--panel)" in STYLE.split(".admin-rag-file-group {", 1)[1].split("}", 1)[0]
    assert ".admin-rag-file-group > summary:focus-visible" in STYLE
