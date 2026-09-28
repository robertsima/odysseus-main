"""File tools reach a managed worktree of the workspace's own repository.

2026-09-28: a Lead Engineer bound to /app/data/development/dog-trainer (Umni)
created agent/umni/bounded-umni-slice with manage_agent_worktree, at
/app/data/agent_worktrees/_repos/umni-50d95770/bounded-umni-slice, then
reported "could not inspect or edit it because the worktree is outside the
active workspace". A worktree of the same repository is now in reach; a
worktree of any other repository, and everything else outside the workspace,
still is not.
"""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from src import tool_execution
from src.agent_worktree import ownership

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(cwd, *args):
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.test",
             "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.test"},
    )


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "core.autocrlf", "false")
    (path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    _git(path, "add", "app.py")
    _git(path, "commit", "-q", "-m", "initial")
    return path


@pytest.fixture
def world(tmp_path, monkeypatch):
    umni = _repo(tmp_path / "development" / "dog-trainer")
    other = _repo(tmp_path / "development" / "log-stream-service")
    trees = tmp_path / "agent_worktrees"
    slice_tree = trees / "_repos" / "umni-50d95770" / "bounded-umni-slice"
    other_tree = trees / "_repos" / "log-stream-1234abcd" / "fix"
    slice_tree.parent.mkdir(parents=True)
    other_tree.parent.mkdir(parents=True)
    _git(umni, "worktree", "add", "-q", "-b", "agent/umni/bounded-umni-slice", str(slice_tree))
    _git(other, "worktree", "add", "-q", "-b", "agent/log-stream-service/fix", str(other_tree))
    monkeypatch.setattr(ownership, "managed_worktree_root", lambda: trees.resolve())
    token = tool_execution._active_workspace.set(os.path.realpath(umni))
    yield {"umni": umni, "other": other, "slice": slice_tree, "other_tree": other_tree}
    tool_execution._active_workspace.reset(token)


def test_a_worktree_of_the_workspace_repository_is_in_reach(world):
    target = world["slice"] / "app.py"
    assert tool_execution._resolve_tool_path(str(target)) == os.path.realpath(target)
    # A new file (not there yet) inside the worktree resolves too.
    new = world["slice"] / "checkins" / "form.py"
    assert tool_execution._resolve_tool_path(str(new)) == os.path.realpath(new)
    # grep/glob/ls take the worktree as a search root.
    assert tool_execution._resolve_search_root(str(world["slice"])) == os.path.realpath(world["slice"])


def test_a_worktree_of_another_repository_stays_out_of_reach(world):
    with pytest.raises(ValueError, match="outside the workspace"):
        tool_execution._resolve_tool_path(str(world["other_tree"] / "app.py"))
    with pytest.raises(ValueError, match="outside the workspace"):
        tool_execution._resolve_tool_path(str(world["other"] / "app.py"))


def test_relative_paths_stay_workspace_relative_and_deny_lists_still_apply(world):
    assert tool_execution._resolve_tool_path("app.py") == os.path.realpath(world["umni"] / "app.py")
    (world["slice"] / ".ssh").mkdir()
    with pytest.raises(ValueError, match="sensitive"):
        tool_execution._resolve_tool_path(str(world["slice"] / ".ssh" / "id_rsa"))


def test_a_forged_pointer_outside_the_root_does_not_qualify(world, tmp_path):
    """Only directories under the worktree root count, whatever .git says."""
    fake = tmp_path / "elsewhere"
    fake.mkdir()
    (fake / ".git").write_text(f"gitdir: {world['umni'] / '.git'}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="outside the workspace"):
        tool_execution._resolve_tool_path(str(fake / "x.py"))


def test_read_and_edit_file_work_in_the_worktree(world):
    from src.agent_tools.filesystem_tools import EditFileTool, ReadFileTool

    target = world["slice"] / "app.py"
    read = asyncio.run(ReadFileTool().execute(json.dumps({"path": str(target)}), {}))
    assert "print('hi')" in json.dumps(read), read
    edited = asyncio.run(EditFileTool().execute(json.dumps(
        {"path": str(target), "old_string": "hi", "new_string": "check-in"}), {}))
    assert edited.get("exit_code", 0) == 0, edited
    assert target.read_text(encoding="utf-8") == "print('check-in')\n"
    # The workspace checkout itself is untouched.
    assert (world["umni"] / "app.py").read_text(encoding="utf-8") == "print('hi')\n"
