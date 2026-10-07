"""With no base branch configured, an agent worktree starts from the repository's own default.

The built-in default base branch was ``dev``, this project's convention. On a
fresh install whose repository has only ``main``, the agent's first worktree
failed with "base branch 'dev' not found" until someone found the setting.
"""
import os
import shutil
import subprocess

import pytest

from src.agent_worktree import service
from src.agent_worktree.config import WorktreeConfig

git_required = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.test",
                        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.test"})


def _cfg(tmp_path, source):
    return WorktreeConfig(
        publish_enabled=False, repo_slug="", source_repo=str(source),
        worktree_root=str(tmp_path / "wt"), state_dir=str(tmp_path / "state"),
        base_branch="", approval_ttl_s=900, api_base="https://api.github.test",
        app_id="", installation_id="", private_key_path="",
        fallback_token_env="ODYSSEUS_AGENT_GITHUB_TOKEN", _fallback_token_present=False,
    )


def _repo(path, branch):
    path.mkdir()
    _git(path, "init", "-q", "-b", branch)
    (path / "README.md").write_text("hello\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


@git_required
async def test_a_clone_starts_from_origin_head(tmp_path):
    upstream = _repo(tmp_path / "upstream", "main")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(upstream), str(clone))
    cfg = _cfg(tmp_path, clone)

    info = await service.ensure_worktree("task", cfg=cfg)

    assert info["exists"] is True
    assert (await service.status(info["branch"], cfg=cfg))["pr_base"] == "main"


@git_required
async def test_a_local_only_repository_starts_from_its_checked_out_branch(tmp_path):
    source = _repo(tmp_path / "source", "trunk")
    cfg = _cfg(tmp_path, source)

    info = await service.ensure_worktree("task", cfg=cfg)

    assert info["exists"] is True
    assert (await service.status(info["branch"], cfg=cfg))["pr_base"] == "trunk"
