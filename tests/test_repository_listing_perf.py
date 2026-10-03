"""manage_git repositories: fast listing, one tree validation per commit.

2026-10-02: the listing took 58 s on the NAS because every checkout re-walked
its HEAD tree and index. These tests build one ~2,000 file repository with five
linked worktrees and count validations.
"""
import os
import shutil
import subprocess
import time

import pytest

from src.agent_worktree import repository_sync as rs

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="git binary required to build fixtures")


def _git(cwd, *args):
    subprocess.run(
        [GIT, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True,
    )


@pytest.fixture
def root(tmp_path, monkeypatch):
    dev = tmp_path / "development"
    dev.mkdir()
    monkeypatch.setattr(rs, "git_repository_roots", lambda: (dev,))
    monkeypatch.setattr(rs, "DATA_DIR", str(tmp_path / "data"))
    import src.constants as constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(rs, "PERSONAL_DIR", str(tmp_path / "data" / "personal"))
    monkeypatch.setattr(rs, "vault_root", lambda: str(tmp_path / "vault"))
    with rs._TREE_LOCK:
        rs._TREE_VERDICTS.clear()
    return dev


def _big_repo(root, files=2000, worktrees=5):
    main = root / "big"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    for d in range(40):
        sub = main / f"pkg{d}"
        sub.mkdir()
        for f in range(files // 40):
            (sub / f"m{f}.py").write_text(f"x = {d}{f}\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "init")
    for i in range(worktrees):
        _git(main, "worktree", "add", "-q", "-b", f"wt{i}", str(root / f"wt{i}"))
    return main


@pytest.mark.asyncio
async def test_listing_validates_each_commit_once(root, monkeypatch):
    """The cache half of the check below, on a small repository so it runs
    on every push. The timing half needs the 2,000-file one."""
    _big_repo(root, files=40)
    (root / "wt2" / "pkg1" / "m1.py").write_text("changed\n")
    calls = []
    real = rs._validate_tree_uncached
    monkeypatch.setattr(
        rs, "_validate_tree_uncached", lambda repo, cid: calls.append(cid) or real(repo, cid)
    )

    rows = await rs.list_repositories()

    assert len(rows) == 6
    assert all(r["ok"] and r["inspected"] for r in rows)
    assert len({c for c in calls}) == len(calls) == 1  # six checkouts, one commit
    by = {os.path.basename(r["repository"]): r for r in rows}
    assert by["wt2"]["dirty"] is True and by["big"]["dirty"] is False
    assert by["wt0"]["linked_worktree"] is True

    await rs.list_repositories()
    assert len(calls) == 1  # still cached


@pytest.mark.nightly  # builds a 2,000-file repository with five worktrees: 12 s in WSL
@pytest.mark.asyncio
async def test_listing_is_fast_and_validates_each_commit_once(root, monkeypatch, capsys):
    _big_repo(root)
    (root / "wt2" / "pkg1" / "m1.py").write_text("changed\n")
    calls = []
    real = rs._validate_tree_uncached
    monkeypatch.setattr(
        rs, "_validate_tree_uncached", lambda repo, cid: calls.append(cid) or real(repo, cid)
    )

    start = time.monotonic()
    rows = await rs.list_repositories()
    cold = time.monotonic() - start

    assert len(rows) == 6
    assert all(r["ok"] and r["inspected"] for r in rows)
    assert len({c for c in calls}) == len(calls) == 1  # six checkouts, one commit
    by = {os.path.basename(r["repository"]): r for r in rows}
    assert by["wt2"]["dirty"] is True and by["big"]["dirty"] is False
    assert by["wt0"]["linked_worktree"] is True
    assert cold < 10
    print(f"\nlisting 6 checkouts, 2000 files: {cold:.2f}s")

    start = time.monotonic()
    await rs.list_repositories()
    assert len(calls) == 1  # still cached
    print(f"warm listing: {time.monotonic() - start:.2f}s")


@pytest.mark.asyncio
async def test_listing_budget_marks_slow_entries_uninspected(root, monkeypatch):
    _big_repo(root, files=40, worktrees=2)
    monkeypatch.setattr(rs, "LISTING_BUDGET_S", 0.3)
    real = rs._status_sync

    def slow(path, **kw):
        if os.path.basename(str(path)) == "wt1":
            time.sleep(1.5)
        return real(path, **kw)

    monkeypatch.setattr(rs, "_status_sync", slow)
    rows = await rs.list_repositories()
    by = {os.path.basename(r["repository"]): r for r in rows}
    assert by["wt1"]["inspected"] is False
    assert by["big"]["inspected"] is True


@pytest.mark.asyncio
async def test_listing_never_runs_the_git_binary(root, monkeypatch):
    """A repository's own .git/config can define a clean filter that `git
    status` would run on the host; the listing stays on dulwich."""
    import subprocess

    _big_repo(root, files=40, worktrees=1)
    (root / "wt0" / "new.txt").write_text("u\n")

    def refuse(*a, **k):
        raise AssertionError("listing ran a subprocess")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    rows = {os.path.basename(r["repository"]): r for r in await rs.list_repositories()}
    assert rows["wt0"]["dirty"] is True and rows["big"]["dirty"] is False


@pytest.mark.asyncio
async def test_symlink_in_tree_is_still_refused(root):
    repo = root / "unsafe"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a")
    # A symlink entry (mode 120000) written straight to the index, so the test
    # also runs where the filesystem cannot create symlinks.
    blob = subprocess.run(
        [GIT, "hash-object", "-w", "--stdin"], cwd=repo, input=b"a.txt",
        check=True, capture_output=True,
    ).stdout.decode().strip()
    _git(repo, "add", "a.txt")
    _git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},link")
    _git(repo, "commit", "-q", "-m", "x")
    with pytest.raises(rs.RepositorySyncError) as exc:
        await rs.repository_status(str(repo))
    assert exc.value.code == "unsafe_tree"
    # Cached refusal is still a refusal.
    with pytest.raises(rs.RepositorySyncError) as exc:
        await rs.repository_status(str(repo))
    assert exc.value.code == "unsafe_tree"
    row = (await rs.list_repositories())[0]
    assert row["ok"] is False and row["code"] == "unsafe_tree"
