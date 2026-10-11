"""A checkout offered to the model as a workspace is one the workspace check accepts.

Bug (2026-10-10): `manage_agent_worktree` made a worktree, the preflight message
listed it as a candidate, and `vet_workspace` refused it ("listed but rejected").
Or it was missing from the list, so a resume said the chat had no workspace.
"""
import os
import importlib
import subprocess

import pytest

from src import worker_preflight as wp


def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """A DATA_DIR with a source repository and the managed worktree root, as in the app."""
    import src.tool_execution as te
    from src import constants

    data = tmp_path / "data"
    source = data / "development" / "main"
    source.mkdir(parents=True)
    _git("init", "-q", cwd=source)
    _git("commit", "--allow-empty", "-qm", "init", cwd=source)
    monkeypatch.setattr(constants, "DATA_DIR", str(data))
    monkeypatch.setenv("ODYSSEUS_AGENT_SOURCE_REPO", str(source))
    monkeypatch.delenv("ODYSSEUS_AGENT_WORKTREE_ROOT", raising=False)
    roots = {"value": [str(data / "development")]}
    monkeypatch.setattr(
        importlib.import_module("src.agent_tools.claude_code_tools"), "_setting",
        lambda key, default=None: roots["value"] if key == "claude_code_repository_roots" else default,
    )
    te._REPO_SUBDIRS_CACHE.clear()
    yield data, source, roots
    te._REPO_SUBDIRS_CACHE.clear()


def _add_worktree(source, path, branch):
    path.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-q", "-b", branch, str(path), cwd=source)
    return str(path.resolve())


def test_a_nested_managed_worktree_is_listed_and_accepted(layout):
    """Worktrees of another repository sit at <root>/_repos/<key>/<name>; discovery stopped one level short."""
    from src.tool_execution import vet_workspace

    data, source, roots = layout
    nested = _add_worktree(source, data / "agent_worktrees" / "_repos" / "other-1" / "feature", "agent/odysseus/n")

    assert nested in wp.known_checkouts()
    assert vet_workspace(nested) == nested


def test_every_listed_candidate_passes_the_workspace_check(layout):
    """A configured root that is DATA_DIR itself lists app-state folders the check refuses."""
    from src.tool_execution import vet_workspace

    data, source, roots = layout
    odd = data / "scratch"
    (odd / ".git").mkdir(parents=True)
    roots["value"] = [str(data)]
    worktree = _add_worktree(source, data / "agent_worktrees" / "slug", "agent/odysseus/s")

    candidates = wp.known_checkouts()

    assert worktree in candidates
    assert all(vet_workspace(path) == os.path.realpath(path) for path in candidates)
    assert str(odd.resolve()) not in candidates


def test_worktrees_under_a_later_root_are_not_crowded_out(layout):
    """The 25-entry cap filled up on the first root, so the managed worktree never reached the list."""
    data, source, roots = layout
    for index in range(30):
        (data / "development" / f"repo-{index:02d}" / ".git").mkdir(parents=True)
    worktree = _add_worktree(source, data / "agent_worktrees" / "consolidation-outcomes-blockers", "agent/odysseus/c")

    assert worktree in wp.known_checkouts()
