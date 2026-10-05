"""A worker whose workspace is a linked git worktree can run git in the sandbox.

2026-10-01: workers in /app/data/agent_worktrees/<slug> got "fatal: not a git
repository: .../.git/worktrees/<slug>" because the shared git dir (in the main
checkout) was never bound. These tests check the bwrap argv only, so they run
on any host.
"""

import dataclasses
import os
import shutil
import subprocess

import pytest

from src import shell_sandbox as sb
from src.agent_worktree import config as wt_config

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=cwd, check=True, capture_output=True)


def _repo(path):
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "f.txt").write_text("x")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "init")
    (path / ".git" / "hooks").mkdir(exist_ok=True)
    return path


@pytest.fixture
def layout(tmp_path, monkeypatch):
    root = tmp_path / "agent_worktrees"
    root.mkdir()
    real_load = wt_config.load_config
    monkeypatch.setattr(wt_config, "load_config",
                        lambda: dataclasses.replace(real_load(), worktree_root=str(root.resolve())))
    monkeypatch.setattr(sb, "network_enabled", lambda: False)
    main = _repo(tmp_path / "development" / "repo")
    return tmp_path, root, main


def _binds(argv, flag):
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == flag]


def test_linked_worktree_workspace_binds_the_shared_git_dir(layout):
    _, root, main = layout
    wt = root / "slug"  # directly under the root, not under _repos/
    _git(main, "worktree", "add", "-q", "-b", "slug", str(wt))
    argv = sb.build_argv(["true"], workspace=str(wt))
    common = os.path.realpath(main / ".git")
    ws = os.path.realpath(wt)
    rw = _binds(argv, "--bind")
    assert (ws, ws) in rw and (common, common) in rw
    # the main checkout's sources are not exposed, only its git dir
    assert not any(src == os.path.realpath(main) for src, _ in rw)
    ro = [src for src, _ in _binds(argv, "--ro-bind")]
    assert os.path.join(common, "hooks") in ro and os.path.join(common, "config") in ro
    # overlays come after the rw bind, or bwrap would shadow them
    assert argv.index(os.path.join(common, "hooks")) > argv.index(common)


def test_relative_gitdir_pointer_still_resolves(layout):
    _, root, main = layout
    wt = root / "rel"
    _git(main, "worktree", "add", "-q", "-b", "rel", str(wt))
    admin = main / ".git" / "worktrees" / "rel"
    (wt / ".git").unlink()  # git hides it on Windows
    (wt / ".git").write_text("gitdir: " + os.path.relpath(admin, wt) + "\n")
    argv = sb.build_argv(["true"], workspace=str(wt))
    common = os.path.realpath(main / ".git")
    assert (common, common) in _binds(argv, "--bind")


def test_main_checkout_workspace_adds_no_git_bind(layout):
    _, root, main = layout
    wt = root / "slug"
    _git(main, "worktree", "add", "-q", "-b", "slug", str(wt))
    argv = sb.build_argv(["true"], workspace=str(main))
    rw = [src for src, _ in _binds(argv, "--bind")]
    # workspace + the managed worktree (flat under the root); .git is inside the workspace
    assert rw == [os.path.realpath(main), os.path.realpath(wt)]


def test_sibling_worktree_is_bound_when_workspace_is_a_worktree(layout):
    _, root, main = layout
    a, b = root / "a", root / "b"
    _git(main, "worktree", "add", "-q", "-b", "a", str(a))
    _git(main, "worktree", "add", "-q", "-b", "b", str(b))
    argv = sb.build_argv(["true"], workspace=str(a))
    rw = [src for src, _ in _binds(argv, "--bind")]
    assert os.path.realpath(b) in rw and os.path.realpath(main / ".git") in rw


def test_unrelated_repository_git_dir_is_never_bound(layout):
    tmp_path, root, main = layout
    other = _repo(tmp_path / "development" / "other")
    wt = root / "o"
    _git(other, "worktree", "add", "-q", "-b", "o", str(wt))
    argv = sb.build_argv(["true"], workspace=str(wt))
    rw = [src for src, _ in _binds(argv, "--bind")]
    assert os.path.realpath(other / ".git") in rw       # its own repository
    assert os.path.realpath(main / ".git") not in rw    # never another one


def test_forged_pointer_to_a_foreign_git_dir_is_refused(layout):
    tmp_path, root, main = layout
    wt = root / "evil"
    wt.mkdir()
    # gitdir lies outside any git dir's worktrees/ layout
    (wt / ".git").write_text(f"gitdir: {tmp_path}\n")
    assert sb.linked_git_binds(str(wt)) == []


def test_symlinked_git_dir_is_not_bound(layout):
    tmp_path, root, main = layout
    wt = root / "sl"
    _git(main, "worktree", "add", "-q", "-b", "sl", str(wt))
    link = tmp_path / "link"
    try:
        os.symlink(main / ".git", link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    (wt / ".git").unlink()
    (wt / ".git").write_text(f"gitdir: {link}/worktrees/sl\n")
    assert sb.linked_git_binds(str(wt)) == []


def test_worktree_under_data_dir_root_is_bound_even_without_repo_roots(layout, monkeypatch):
    # The 2026-10-01 second failure: workspace_problem refused a worktree inside
    # DATA_DIR that _repository_data_subdirs did not list.
    tmp_path, root, main = layout
    wt = root / "slug"
    _git(main, "worktree", "add", "-q", "-b", "slug", str(wt))
    import src.constants as constants
    import src.tool_execution as te

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(te, "_repository_data_subdirs", lambda data: ())
    assert os.path.realpath(wt) in sb.workspace_worktrees(str(main))
