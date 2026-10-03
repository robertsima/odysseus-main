"""The file tools stay inside the chat's workspace folder."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from src import tool_execution

pytestmark = pytest.mark.security


@pytest.fixture
def folders(tmp_path, monkeypatch):
    # With auth off the caller is the single local user, who may use every
    # tool, so only the path decides whether the read goes through.
    monkeypatch.setenv("AUTH_ENABLED", "false")
    workspace = tmp_path / "ws"
    sibling = tmp_path / "ws-evil"
    workspace.mkdir()
    sibling.mkdir()
    (workspace / "notes.txt").write_text("inside the workspace", encoding="utf-8")
    (sibling / "secret.txt").write_text("sibling secret", encoding="utf-8")
    return workspace, sibling


def _read_file(path, workspace):
    block = SimpleNamespace(tool_type="read_file", content=json.dumps({"path": str(path)}))
    _, result = asyncio.run(tool_execution.execute_tool_block(
        block, workspace=str(workspace), security_context=tool_execution.NO_TOOL_SECURITY_CONTEXT,
    ))
    return result


def test_a_sibling_folder_whose_name_starts_with_the_workspace_name_is_refused(folders):
    workspace, sibling = folders

    result = _read_file(sibling / "secret.txt", workspace)

    assert "sibling secret" not in json.dumps(result)
    assert "outside the workspace" in result.get("error", "")


def test_a_file_inside_the_workspace_is_read(folders):
    workspace, _ = folders

    result = _read_file(workspace / "notes.txt", workspace)

    assert result.get("output") == "inside the workspace"
