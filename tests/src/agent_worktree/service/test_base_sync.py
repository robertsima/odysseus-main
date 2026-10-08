"""manage_agent_worktree sync: merge the base's remote tip into a managed worktree.

2026-10-07/08: agents could not bring the base branch into their worktree, so
every "PR is behind / has conflicts" stalled 35-70 minutes until a person
finished it on GitHub. These run real git in temporary repositories; only the
fetch (the network) is replaced by a fetch from a local upstream repository.
"""

import os
import subprocess

import pytest

from src.agent_worktree import service
from src.agent_worktree.config import WorktreeConfig
from tests.src.agent_worktree.gitcmd.test_agent_worktree_service import git_required

pytestmark = git_required

_ENV = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.test"}


def git(cwd, *args):
    out = subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd, check=True,
                         capture_output=True, text=True, env={**os.environ, **_ENV})
    return out.stdout.strip()


def write(root, relative, text):
    target = os.path.join(str(root), *relative.split("/"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def read(root, relative):
    with open(os.path.join(str(root), *relative.split("/")), encoding="utf-8") as fh:
        return fh.read().replace("\r\n", "\n")


def upstream_commit(upstream, relative, text, message="base change", branch="dev"):
    git(upstream, "checkout", "-q", branch)
    write(upstream, relative, text)
    git(upstream, "add", "-A")
    git(upstream, "commit", "-q", "-m", message)
    return git(upstream, "rev-parse", "HEAD")


@pytest.fixture
def upstream(tmp_path):
    root = tmp_path / "upstream"
    root.mkdir()
    git(root, "init", "-q", "-b", "dev")
    write(root, "README.md", "one\ntwo\nthree\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def repo(tmp_path, upstream):
    root = tmp_path / "repo"
    git(tmp_path, "clone", "-q", str(upstream), str(root))
    return root


@pytest.fixture
def cfg(tmp_path, repo):
    return WorktreeConfig(
        publish_enabled=True, repo_slug="acme/widgets", source_repo=str(repo),
        worktree_root=str(tmp_path / "wt"), state_dir=str(tmp_path / "state"),
        base_branch="dev", approval_ttl_s=900, api_base="https://api.github.test",
        app_id="", installation_id="", private_key_path="",
        fallback_token_env="ODYSSEUS_AGENT_GITHUB_TOKEN", _fallback_token_present=True,
    )


@pytest.fixture
def local_fetch(monkeypatch):
    """The base fetch reads the local upstream instead of GitHub."""
    calls = []

    async def fetch(rcfg, base_branch, token):
        calls.append(base_branch)
        git(rcfg.source_repo, "fetch", "-q", "origin",
            f"+refs/heads/{base_branch}:refs/remotes/origin/{base_branch}")
        return {"remote": "origin",
                "sha": git(rcfg.source_repo, "rev-parse", f"refs/remotes/origin/{base_branch}")}

    monkeypatch.setattr(service, "_fetch_base", fetch)
    return calls


async def start_with_change(cfg, relative="feature.py", text="x = 1\n", name="task"):
    info = await service.ensure_worktree(name, cfg=cfg)
    write(info["path"], relative, text)
    await service.commit(name, "agent change", cfg=cfg)
    return info["path"]


async def test_sync_reports_up_to_date_when_the_base_did_not_move(cfg, local_fetch):
    path = await start_with_change(cfg)
    head = git(path, "rev-parse", "HEAD")
    out = await service.sync("task", cfg=cfg)
    assert out["result"] == "already_up_to_date"
    assert out["head_sha"] == head == git(path, "rev-parse", "HEAD")
    assert local_fetch == ["dev"]


async def test_sync_merges_the_new_base_tip_with_both_parents(cfg, upstream, local_fetch):
    path = await start_with_change(cfg)
    before = git(path, "rev-parse", "HEAD")
    base_tip = upstream_commit(upstream, "docs/new.md", "added on dev\n")

    out = await service.sync("task", cfg=cfg, expected_head=before)

    assert out["result"] == "merged"
    assert out["base_sha"] == base_tip
    head = git(path, "rev-parse", "HEAD")
    assert out["head_sha"] == head
    assert git(path, "rev-list", "--parents", "-n", "1", head).split()[1:] == [before, base_tip]
    assert read(path, "docs/new.md") == "added on dev\n"
    assert git(path, "status", "--porcelain") == ""


async def test_sync_refuses_a_dirty_tree_and_a_moved_head_before_fetching(cfg, upstream, local_fetch):
    path = await start_with_change(cfg)
    upstream_commit(upstream, "docs/new.md", "added on dev\n")
    write(path, "scratch.txt", "unsaved\n")
    with pytest.raises(service.WorktreeError) as dirty:
        await service.sync("task", cfg=cfg)
    assert dirty.value.code == "WORKTREE_DIRTY"
    os.remove(os.path.join(path, "scratch.txt"))
    with pytest.raises(service.WorktreeError) as moved:
        await service.sync("task", cfg=cfg, expected_head="f" * 40)
    assert moved.value.code == "HEAD_MISMATCH"
    assert local_fetch == []
    assert not os.path.exists(os.path.join(path, "docs", "new.md"))


async def test_sync_conflict_lists_files_and_hunks_and_leaves_the_merge_open(cfg, upstream, local_fetch):
    path = await start_with_change(cfg, "README.md", "one\nTWO from the branch\nthree\n")
    before = git(path, "rev-parse", "HEAD")
    upstream_commit(upstream, "README.md", "one\ntwo from dev\nthree\n")

    out = await service.sync("task", cfg=cfg)

    assert out["result"] == "conflicted"
    assert out["head_sha"] == before
    assert out["conflicted_paths"] == ["README.md"]
    row = out["conflicted_files"][0]
    assert row["kind"] == "both modified"
    assert "TWO from the branch" in row["hunks"] and "two from dev" in row["hunks"]
    assert "<<<<<<<" in row["hunks"] and ">>>>>>>" in row["hunks"]
    assert out["truncated"] is False
    # The merge is still in progress for the agent to resolve.
    assert git(path, "rev-parse", "-q", "--verify", "MERGE_HEAD")


async def test_sync_conflict_hunks_are_bounded_and_say_so(cfg, upstream, local_fetch, monkeypatch):
    monkeypatch.setattr(service, "SYNC_FILE_HUNK_CHARS", 60)
    path = await start_with_change(cfg, "README.md", "one\n" + "branch line\n" * 50 + "three\n")
    upstream_commit(upstream, "README.md", "one\n" + "dev line\n" * 50 + "three\n")
    out = await service.sync("task", cfg=cfg)
    row = out["conflicted_files"][0]
    assert len(row["hunks"]) <= 60
    assert row["truncated"] is True and out["truncated"] is True
    assert git(path, "rev-parse", "-q", "--verify", "MERGE_HEAD")


async def test_sync_abort_drops_the_merge_and_restores_the_head(cfg, upstream, local_fetch):
    path = await start_with_change(cfg, "README.md", "one\nbranch\nthree\n")
    before = git(path, "rev-parse", "HEAD")
    upstream_commit(upstream, "README.md", "one\ndev\nthree\n")
    assert (await service.sync("task", cfg=cfg))["result"] == "conflicted"

    out = await service.sync("task", cfg=cfg, abort=True)

    assert out["result"] == "aborted" and out["head_sha"] == before
    assert git(path, "status", "--porcelain") == ""
    assert read(path, "README.md") == "one\nbranch\nthree\n"
    assert (await service.sync("task", cfg=cfg, abort=True))["result"] == "no_merge_in_progress"


async def test_commit_refuses_conflict_markers_then_concludes_the_merge(cfg, upstream, local_fetch):
    path = await start_with_change(cfg, "README.md", "one\nbranch\nthree\n")
    before = git(path, "rev-parse", "HEAD")
    base_tip = upstream_commit(upstream, "README.md", "one\ndev\nthree\n")
    assert (await service.sync("task", cfg=cfg))["result"] == "conflicted"

    with pytest.raises(service.WorktreeError) as caught:
        await service.commit("task", "merge dev", cfg=cfg)
    assert caught.value.code == "CONFLICT_MARKERS"
    assert "README.md:2" in str(caught.value)
    assert git(path, "rev-parse", "HEAD") == before

    write(path, "README.md", "one\nbranch and dev\nthree\n")
    out = await service.commit("task", "merge dev", cfg=cfg)

    assert out["committed"] is True and out["merge_concluded"] is True
    assert out["parents"] == [before, base_tip]
    assert not subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=path,
                              capture_output=True).stdout.strip()


async def test_commit_concludes_a_merge_resolved_to_the_branch_side(cfg, upstream, local_fetch):
    # Keeping HEAD's version leaves nothing in `git status`; the merge must
    # still be committed or the base never becomes a parent.
    path = await start_with_change(cfg, "README.md", "one\nbranch\nthree\n")
    before = git(path, "rev-parse", "HEAD")
    base_tip = upstream_commit(upstream, "README.md", "one\ndev\nthree\n")
    await service.sync("task", cfg=cfg)
    write(path, "README.md", "one\nbranch\nthree\n")

    out = await service.commit("task", "merge dev keeping ours", cfg=cfg)

    assert out["committed"] is True and out["parents"] == [before, base_tip]
