"""manage_git / repo_status on linked worktrees (`.git` is a `gitdir:` file).

On 2026-09-28 every read-only look at the Umni project's linked worktrees
(umni-main-audit, dog-trainer-checkins, ...) failed with "only physical
checkouts with a .git directory are supported". Read-only actions now accept a
linked worktree whose pointer chain is verified hop by hop and whose main
repository passes the same approval as a checkout named directly; writes keep
requiring the physical checkout, and every pointer trick is still refused.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from src.agent_worktree import repository_local, repository_sync as rs

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(cwd, *args):
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.test",
             "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.test"},
    )


def _main_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "core.autocrlf", "false")
    (path / "README.md").write_text("one\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "initial")
    _git(path, "remote", "add", "origin", "https://github.com/acme/example.git")
    return path


@pytest.fixture
def roots(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(rs, "repository_roots", lambda: (root,))
    monkeypatch.setattr(rs, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(rs, "PERSONAL_DIR", str(tmp_path / "data" / "personal_docs"))
    monkeypatch.setenv("ODYSSEUS_AGENT_SOURCE_REPO", str(tmp_path / "odysseus"))
    monkeypatch.setenv("ODYSSEUS_AGENT_WORKTREE_ROOT", str(tmp_path / "agent_worktrees"))
    monkeypatch.setenv("ODYSSEUS_AGENT_STATE_DIR", str(tmp_path / "state"))
    return root


@pytest.fixture
def linked(roots):
    main = _main_repo(roots / "dog-trainer")
    _git(main, "worktree", "add", "-q", "-b", "audit", str(roots / "umni-main-audit"))
    return main, roots / "umni-main-audit"


async def test_status_of_a_linked_worktree_names_its_main_repository(linked):
    main, worktree = linked
    (worktree / "notes.txt").write_text("x\n", encoding="utf-8")
    result = await rs.repository_status(str(worktree))
    assert result["ok"] is True
    assert result["branch"] == "audit"
    assert result["linked_worktree"] is True
    assert Path(result["main_repository"]) == main.resolve()
    # The `.git` pointer file is metadata, not an untracked file.
    assert result["dirty"]["untracked"] == ["notes.txt"]


@pytest.mark.parametrize("action", sorted(rs.LINKED_READ_ONLY_ACTIONS))
async def test_read_only_manage_git_actions_work_on_a_linked_worktree(linked, action):
    _main, worktree = linked
    result = await repository_local.execute_local(action, str(worktree))
    assert result["repository"] == str(worktree.resolve())


async def test_write_actions_on_a_linked_worktree_name_the_main_repository(linked):
    main, worktree = linked
    (worktree / "new.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(rs.RepositorySyncError) as exc:
        await repository_local.execute_local("stage", str(worktree), paths=["new.txt"])
    assert exc.value.code == "linked_worktree_read_only"
    assert str(main.resolve()) in str(exc.value)


async def test_writes_in_a_managed_worktree_point_at_its_commit_action(roots, monkeypatch):
    # 2026-09-29: workers standing in a managed Umni worktree were told to go
    # and start an isolated worktree; they needed the commit call for this one.
    main = _main_repo(roots / "dog-trainer")
    managed = roots / "agent_worktrees" / "_repos" / "umni-1234" / "fix-ci"
    managed.parent.mkdir(parents=True)
    _git(main, "worktree", "add", "-q", "-b", "agent/umni/fix-ci", str(managed))
    monkeypatch.setenv("ODYSSEUS_AGENT_WORKTREE_ROOT", str(roots / "agent_worktrees"))
    (managed / "new.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(rs.RepositorySyncError) as exc:
        await repository_local.execute_local("stage", str(managed), paths=["new.txt"])
    assert exc.value.code == "linked_worktree_read_only"
    message = str(exc.value)
    assert "managed worktree" in message
    assert "manage_agent_worktree action=commit" in message
    assert "name=fix-ci" in message
    assert str(main.resolve()) in message


async def test_repositories_listing_reports_linked_worktrees(linked):
    main, worktree = linked
    rows = {Path(row["repository"]): row for row in await rs.list_repositories()}
    assert rows[worktree.resolve()]["ok"] is True
    assert Path(rows[worktree.resolve()]["main_repository"]) == main.resolve()
    assert rows[main.resolve()].get("linked_worktree") is None


def test_a_linked_worktree_of_a_repository_outside_the_roots_is_refused(roots, tmp_path):
    outside = _main_repo(tmp_path / "outside" / "secret-project")
    _git(outside, "worktree", "add", "-q", "-b", "peek", str(roots / "peek"))
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_path(str(roots / "peek"), allow_linked=True)
    assert exc.value.code == "outside_roots"


def test_a_copied_pointer_is_refused_by_the_back_pointer(linked, roots):
    _main, worktree = linked
    impostor = roots / "impostor"
    impostor.mkdir()
    (impostor / ".git").write_bytes((worktree / ".git").read_bytes())
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_path(str(impostor), allow_linked=True)
    assert exc.value.code == "unsafe_metadata"


def test_a_commondir_redirected_to_another_repository_is_refused(linked, roots):
    main, worktree = linked
    other = _main_repo(roots / "other")
    gitdir = main / ".git" / "worktrees" / "umni-main-audit"
    (gitdir / "commondir").write_text(str(other / ".git") + "\n", encoding="utf-8")
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_path(str(worktree), allow_linked=True)
    assert exc.value.code == "unsafe_metadata"


def test_a_pointer_straight_at_a_main_gitdir_is_refused(linked, roots):
    main, _worktree = linked
    fake = roots / "fake"
    fake.mkdir()
    (fake / ".git").write_text(f"gitdir: {main / '.git'}\n", encoding="utf-8")
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_path(str(fake), allow_linked=True)
    assert exc.value.code == "unsupported_checkout"


def test_per_worktree_config_is_refused(linked):
    main, worktree = linked
    gitdir = main / ".git" / "worktrees" / "umni-main-audit"
    (gitdir / "config.worktree").write_text("[core]\n\thooksPath = /tmp/x\n", encoding="utf-8")
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_path(str(worktree), allow_linked=True)
    assert exc.value.code == "unsafe_metadata"


def test_the_main_repository_metadata_rules_still_apply(linked):
    main, worktree = linked
    with open(main / ".git" / "config", "a", encoding="utf-8") as fh:
        fh.write("[core]\n\thooksPath = /tmp/hooks\n")
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_path(str(worktree), allow_linked=True)
    assert exc.value.code == "unsafe_config"


def test_a_symlinked_pointer_file_is_refused(linked, roots, tmp_path):
    _main, worktree = linked
    elsewhere = tmp_path / "pointer"
    elsewhere.write_bytes((worktree / ".git").read_bytes())
    target = roots / "linkdir"
    target.mkdir()
    try:
        (target / ".git").symlink_to(elsewhere)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    with pytest.raises(rs.RepositorySyncError):
        rs._validate_path(str(target), allow_linked=True)


def test_managed_worktrees_of_the_configured_source_repository_are_readable(roots, tmp_path):
    """The source checkout sits outside the roots; its managed worktrees do not."""
    source = _main_repo(tmp_path / "odysseus")
    managed = tmp_path / "agent_worktrees" / "task"
    managed.parent.mkdir()
    _git(source, "worktree", "add", "-q", "-b", "agent/odysseus/task", str(managed))
    assert rs._validate_path(str(managed), allow_linked=True) == managed.resolve()
    # ...but a linked worktree of the source checkout is still not writable here.
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_path(str(managed))
    assert exc.value.code == "linked_worktree_read_only"
