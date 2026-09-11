"""A file tool that refuses a vault-looking path must say where the vault is.

The agent wrote to `/app/workspace/AI Mind/Projects/Note.md`; `ls` and
`write_file` both refused it as outside the allowed roots, and the model then
wrote the note with a shell one-liner, outside the vault. The rejection now
names the real folder so the next call can succeed.
"""
import asyncio
import importlib
import os

import pytest


@pytest.fixture
def vault(tmp_path, monkeypatch):
    # Resolve the module at fixture time, not at import time: other test
    # files re-import `src.tool_execution`, and the tools under test look it
    # up from sys.modules on every call, so a module object captured at
    # collection can be a stale twin the patches never reach.
    te = importlib.import_module("src.tool_execution")
    root = tmp_path / "personal_docs"
    (root / "AI Mind" / "Projects").mkdir(parents=True)
    (root / "Journal").mkdir()
    monkeypatch.setattr(te, "_personal_docs_root", lambda: str(root))
    monkeypatch.setattr(te, "get_active_workspace", lambda: None)
    return root, te


def test_rejected_vault_path_suggests_the_real_folder(vault):
    root, te = vault
    with pytest.raises(ValueError) as exc:
        te._resolve_tool_path("/app/workspace/AI Mind/Projects/Software Delivery Agent System.md")
    msg = str(exc.value)
    assert "outside the allowed roots" in msg
    assert str(root) in msg
    assert os.path.join(str(root), "AI Mind", "Projects", "Software Delivery Agent System.md") in msg


def test_suggestion_is_case_insensitive_and_keeps_the_rest_of_the_path(vault):
    root, te = vault
    hint = te._personal_docs_suggestion("/somewhere/ai mind/Notes/x.md")
    assert os.path.join(str(root), "AI Mind", "Notes", "x.md") in hint


def test_paths_that_name_no_vault_folder_get_no_hint(vault):
    root, te = vault
    assert te._personal_docs_suggestion("/etc/passwd") == ""
    with pytest.raises(ValueError) as exc:
        te._resolve_tool_path("/etc/passwd")
    assert "did you mean" not in str(exc.value)


def test_hint_also_applies_when_a_workspace_is_bound(vault, tmp_path, monkeypatch):
    root, te = vault
    ws = tmp_path / "repo"
    ws.mkdir()
    monkeypatch.setattr(te, "get_active_workspace", lambda: str(ws))
    with pytest.raises(ValueError) as exc:
        te._resolve_tool_path("/app/workspace/Journal/today.md")
    msg = str(exc.value)
    assert "outside the workspace" in msg
    assert os.path.join(str(root), "Journal", "today.md") in msg


def test_ls_error_carries_the_hint(vault):
    root, _ = vault
    from src.agent_tools.filesystem_tools import LsTool

    out = asyncio.run(LsTool().execute('{"path": "/app/workspace/AI Mind"}', {}))
    assert out["exit_code"] == 1
    assert os.path.join(str(root), "AI Mind") in out["error"]
