"""Vault notes that share a file name, and the document-tool not-found hint.

2026-09-28: two vault notes of the same name — one at the vault root, one
under ``AI Mind/Business Ideas`` — were both edited (08:14:27 and 08:17:48).
The model chose a path by name and nothing told it a second note existed.
Separately, ``update_document`` on a vault note's title only said "not found".
"""

import asyncio
import json
import os

import pytest

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

import src.agent_tools.document_tools as dt  # noqa: E402
import src.agent_tools.filesystem_tools as fst  # noqa: E402
from src.agent_tools import TOOL_HANDLERS  # noqa: E402


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "personal_docs"
    (root / "AI Mind" / "Business Ideas").mkdir(parents=True)
    (root / "Journal").mkdir()
    (root / ".trash").mkdir()
    (root / "Business Ideas.md").write_text("root note\n", encoding="utf-8")
    (root / "AI Mind" / "Business Ideas" / "business ideas.md").write_text("nested note\n", encoding="utf-8")
    (root / "Journal" / "Unique.md").write_text("only one\n", encoding="utf-8")
    (root / ".trash" / "Business Ideas.md").write_text("deleted\n", encoding="utf-8")

    import src.constants as constants
    import src.rag_sensitivity as rs
    import src.tool_execution as te

    monkeypatch.setattr(constants, "PERSONAL_DIR", str(root))
    monkeypatch.setattr(rs, "vault_root", lambda: str(root))
    monkeypatch.setattr(rs, "assert_vault_writable", lambda *a, **k: None)
    monkeypatch.setattr(te, "_resolve_tool_path", lambda raw, **k: os.path.abspath(raw))
    return root


def _run(tool, args):
    return asyncio.run(TOOL_HANDLERS[tool](json.dumps(args), {"owner": "alice"}))


def test_warning_names_the_other_same_named_note(vault):
    warning = fst._same_name_vault_warning(str(vault / "Business Ideas.md"))
    assert "AI Mind/Business Ideas/business ideas.md" in warning
    assert "This call changed Business Ideas.md only" in warning
    assert ".trash" not in warning, "hidden folders are not notes"


def test_no_warning_for_a_unique_note_or_outside_the_vault(vault, tmp_path):
    assert fst._same_name_vault_warning(str(vault / "Journal" / "Unique.md")) == ""
    outside = tmp_path / "workspace" / "Business Ideas.md"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")
    assert fst._same_name_vault_warning(str(outside)) == ""


def test_edit_file_result_carries_the_warning(vault):
    nested = vault / "AI Mind" / "Business Ideas" / "business ideas.md"
    result = _run("edit_file", {"path": str(nested), "old_string": "nested", "new_string": "edited"})
    assert result["exit_code"] == 0, result
    first, _, rest = result["output"].partition("\n")
    assert first.startswith("Edited ")
    assert "[warning] Another vault note has the same file name: Business Ideas.md" in rest
    assert nested.read_text(encoding="utf-8") == "edited note\n"
    assert (vault / "Business Ideas.md").read_text(encoding="utf-8") == "root note\n"


def test_write_file_result_carries_the_warning(vault):
    result = _run("write_file", {"path": str(vault / "Business Ideas.md"), "content": "new body\n"})
    assert result["exit_code"] == 0, result
    assert "AI Mind/Business Ideas/business ideas.md" in result["output"]


def test_write_file_of_a_unique_note_has_no_warning(vault):
    result = _run("write_file", {"path": str(vault / "Journal" / "New.md"), "content": "x"})
    assert result["exit_code"] == 0
    assert "[warning]" not in result["output"]


# ── update_document not-found hint ────────────────────────────────────────


@pytest.fixture
def db(monkeypatch, app_db):
    import core.database as core_database
    import src.database as legacy_database

    SessionLocal = app_db.SessionLocal
    monkeypatch.setattr(core_database, "SessionLocal", SessionLocal)
    monkeypatch.setattr(legacy_database, "SessionLocal", SessionLocal)
    monkeypatch.setattr(dt, "_missing_document_upload", lambda owner, content: None)
    dt.clear_active_document()
    yield SessionLocal
    dt.clear_active_document()


def test_update_document_not_found_points_at_edit_file(db):
    from src.tool_schemas import function_call_to_tool_block

    block = function_call_to_tool_block(
        "update_document", json.dumps({"document_id": "Business Ideas", "content": "x"}),
    )
    result = asyncio.run(TOOL_HANDLERS["update_document"](block.content, {"owner": "alice", "session_id": "c"}))
    assert result["exit_code"] == 1
    assert "was not found among your documents" in result["error"]
    assert "Vault notes are files — edit them by path with edit_file." in result["error"]


def test_sealed_target_not_found_carries_the_hint_too(db):
    result = asyncio.run(TOOL_HANDLERS["update_document"](
        "new body", {"owner": "alice", "session_id": "c", "doc_id": "missing-doc-id"},
    ))
    assert result["exit_code"] == 1
    assert dt.VAULT_NOTE_HINT in result["error"]
