"""Per-path vault policy matrix for the local filesystem tool surface."""

import json

import pytest

from src.agent_tools.filesystem_tools import (
    ApplyPatchTool,
    EditFileTool,
    LsTool,
    ReadFileTool,
    WriteFileTool,
)


@pytest.fixture
def vault_matrix(tmp_path, monkeypatch):
    data = tmp_path / "data"
    vault = data / "personal_docs"
    workspace = tmp_path / "unrelated-workspace"
    workspace.mkdir(parents=True)
    (workspace / "app.py").write_text("workspace\n", encoding="utf-8")
    for folder in ("AI Mind", "Vault Mind", "Journal"):
        target = vault / folder
        target.mkdir(parents=True)
        (target / "note.md").write_text(f"---\ntitle: {folder}\n---\nold value\n", encoding="utf-8")

    settings = {
        "vault_directory": str(vault),
        "vault_default_sensitivity": "public",
        "vault_folder_sensitivity": {
            "AI Mind": {"sensitivity": "public", "readonly": False},
            "Vault Mind": {"sensitivity": "public", "readonly": True},
            "Journal": {"sensitivity": "private", "readonly": True},
        },
        "tool_path_extra_roots": [],
    }
    monkeypatch.setattr("src.constants.DATA_DIR", str(data), raising=False)
    monkeypatch.setattr("src.constants.PERSONAL_DIR", str(vault), raising=False)
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: settings.get(key, default))

    from src.tool_execution import _active_workspace
    token = _active_workspace.set(str(workspace))
    yield vault, workspace
    _active_workspace.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "folder,allow_private,read_ok,write_ok",
    [
        ("AI Mind", False, True, True),
        ("Vault Mind", False, True, False),
        ("Journal", False, False, False),
        ("Journal", True, True, False),
    ],
)
async def test_read_edit_write_matrix(vault_matrix, folder, allow_private, read_ok, write_ok):
    vault, _workspace = vault_matrix
    path = vault / folder / "note.md"
    read = await ReadFileTool().execute(json.dumps({"path": str(path)}), {"allow_private": allow_private})
    assert (read["exit_code"] == 0) is read_ok

    edit = await EditFileTool().execute(json.dumps({
        "path": str(path), "old_string": "old value", "new_string": "edited value",
    }), {})
    assert (edit["exit_code"] == 0) is write_ok

    new_path = vault / folder / "created.md"
    write = await WriteFileTool().execute(json.dumps({
        "path": str(new_path), "content": "created\n",
    }), {})
    assert (write["exit_code"] == 0) is write_ok
    assert new_path.exists() is write_ok


@pytest.mark.asyncio
@pytest.mark.parametrize("folder,ok", [("AI Mind", True), ("Vault Mind", False), ("Journal", False)])
async def test_apply_patch_obeys_folder_write_policy(vault_matrix, folder, ok):
    vault, _workspace = vault_matrix
    path = vault / folder / "note.md"
    patch = (
        "*** Begin Patch\n"
        f"*** Update File: {path}\n"
        "@@\n"
        "-old value\n"
        "+patched value\n"
        "*** End Patch"
    )
    result = await ApplyPatchTool().execute(patch, {})
    assert (result["exit_code"] == 0) is ok
    assert ("patched value" in path.read_text(encoding="utf-8")) is ok


@pytest.mark.asyncio
async def test_root_listing_filters_private_folder_unless_granted(vault_matrix):
    vault, _workspace = vault_matrix
    public = await LsTool().execute(json.dumps({"path": str(vault)}), {"allow_private": False})
    assert public["exit_code"] == 0
    assert "AI Mind/" in public["output"] and "Vault Mind/" in public["output"]
    assert "Journal/" not in public["output"]

    private = await LsTool().execute(json.dumps({"path": str(vault)}), {"allow_private": True})
    assert private["exit_code"] == 0 and "Journal/" in private["output"]


@pytest.mark.asyncio
async def test_personal_root_aliases_work_with_unrelated_workspace(vault_matrix):
    vault, workspace = vault_matrix
    absolute = await ReadFileTool().execute(str(vault / "AI Mind" / "note.md"), {})
    relative = await ReadFileTool().execute("AI Mind/note.md", {})
    workspace_file = await ReadFileTool().execute("app.py", {})
    assert absolute["exit_code"] == workspace_file["exit_code"] == 0
    # Relative paths remain workspace-relative. A coincidental folder name in
    # another workspace must not alias into personal documents; callers use
    # the canonical mounted path returned by vault search/listing.
    assert relative["exit_code"] == 1 and "not found" in relative["error"]
    assert "old value" in absolute["output"]
    assert workspace_file["output"] == "workspace\n"


@pytest.mark.asyncio
async def test_frontmatter_private_override_and_readonly_are_both_enforced(vault_matrix):
    vault, _workspace = vault_matrix
    private_ai = vault / "AI Mind" / "private.md"
    private_ai.write_text("---\nsensitivity: private\n---\nsecret\n", encoding="utf-8")
    denied = await ReadFileTool().execute(str(private_ai), {"allow_private": False})
    granted = await ReadFileTool().execute(str(private_ai), {"allow_private": True})
    assert denied["exit_code"] == 1 and granted["exit_code"] == 0

    blocked_write = await EditFileTool().execute(json.dumps({
        "path": str(vault / "Journal" / "note.md"),
        "old_string": "old value", "new_string": "changed",
    }), {})
    assert blocked_write["exit_code"] == 1
    assert (
        "sensitive directory" in blocked_write["error"]
        or "readonly" in blocked_write["error"]
    )
