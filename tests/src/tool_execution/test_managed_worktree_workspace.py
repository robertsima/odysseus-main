"""vet_workspace accepts a managed worktree the moment `manage_agent_worktree` creates it.

Bug (2026-10-10): the repository-root allowlist was cached for 5 seconds. A path
check made before the worktree root existed cached "no readable root", so the
first worktree created in the next 5 seconds was refused as app state.
"""
import subprocess

import pytest

pytestmark = pytest.mark.security


def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   cwd=cwd, check=True, capture_output=True)


def test_a_worktree_made_right_after_a_path_check_is_accepted(tmp_path, monkeypatch):
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
    monkeypatch.setattr(
        "src.agent_tools.claude_code_tools._setting",
        lambda key, default=None: [str(data / "development")] if key == "claude_code_repository_roots" else default,
    )
    te._REPO_SUBDIRS_CACHE.clear()
    assert te.vet_workspace(str(source))  # primes the cache while agent_worktrees does not exist

    worktree = data / "agent_worktrees" / "consolidation-outcomes-blockers"
    worktree.parent.mkdir()
    _git("worktree", "add", "-q", "-b", "agent/odysseus/c", str(worktree), cwd=source)

    assert te.vet_workspace(str(worktree)) == str(worktree.resolve())
    te._REPO_SUBDIRS_CACHE.clear()
