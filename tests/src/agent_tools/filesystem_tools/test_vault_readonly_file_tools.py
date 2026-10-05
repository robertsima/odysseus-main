"""Agent file mutations respect vault folder read-only policy."""

import json

import pytest

import src.rag_sensitivity as sensitivity
import src.tool_execution as tool_execution
from src.agent_tools.filesystem_tools import ApplyPatchTool, EditFileTool, WriteFileTool


@pytest.fixture
def readonly_vault(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    journal = vault / "Journal"
    journal.mkdir(parents=True)
    monkeypatch.setattr(sensitivity, "vault_root", lambda: str(vault))
    monkeypatch.setattr(
        sensitivity,
        "_safe_folder_policy_map",
        lambda: ({"Journal": sensitivity.FolderPolicy(readonly=True)}, True),
    )
    monkeypatch.setattr(tool_execution, "_resolve_tool_path", lambda path: str(vault / path))
    return vault, journal


@pytest.mark.asyncio
async def test_write_file_refuses_readonly_folder(readonly_vault):
    _vault, journal = readonly_vault
    result = await WriteFileTool().execute(
        json.dumps({"path": "Journal/new.md", "content": "new"}), {}
    )
    assert result["exit_code"] == 1
    assert "readonly" in result["error"]
    assert not (journal / "new.md").exists()


@pytest.mark.asyncio
async def test_edit_file_refuses_readonly_folder(readonly_vault):
    _vault, journal = readonly_vault
    path = journal / "existing.md"
    path.write_text("old", encoding="utf-8")
    result = await EditFileTool().execute(
        json.dumps({"path": "Journal/existing.md", "old_string": "old", "new_string": "new"}),
        {},
    )
    assert result["exit_code"] == 1
    assert "readonly" in result["error"]
    assert path.read_text(encoding="utf-8") == "old"


@pytest.mark.asyncio
async def test_apply_patch_refuses_readonly_folder(readonly_vault):
    _vault, journal = readonly_vault
    path = journal / "existing.md"
    path.write_text("old\n", encoding="utf-8")
    patch = """*** Begin Patch
*** Update File: Journal/existing.md
@@
-old
+new
*** End Patch"""
    result = await ApplyPatchTool().execute(patch, {})
    assert result["exit_code"] == 1
    assert "readonly" in result["error"]
    assert path.read_text(encoding="utf-8") == "old\n"
