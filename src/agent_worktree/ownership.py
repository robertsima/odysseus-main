"""Which managed worktrees belong to a workspace.

A managed worktree (``manage_agent_worktree start``) lives under the worktree
root, outside the checkout it was made from. It belongs to a workspace when
its repository does: the worktree's shared ``.git`` is (or lives inside) the
workspace's checkout. Claude Code delegation and the file tools both use this
to reach a worktree of the repository a chat or worker is bound to, and
nothing else.

Read from the filesystem only (``.git`` pointer files and ``commondir``);
git is never run here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def git_common_dir(path: Path) -> Optional[Path]:
    """The shared ``.git`` directory of a checkout or linked worktree."""
    git = path / ".git"
    try:
        if git.is_dir():
            return git.resolve()
        if not git.is_file():
            return None
        pointer = git.read_text(encoding="utf-8", errors="replace").strip()
        if not pointer.startswith("gitdir:"):
            return None
        gitdir = Path(pointer.split(":", 1)[1].strip())
        if not gitdir.is_absolute():
            gitdir = path / gitdir
        gitdir = gitdir.resolve()
        common = gitdir / "commondir"
        if common.is_file():
            target = Path(common.read_text(encoding="utf-8", errors="replace").strip())
            return (target if target.is_absolute() else gitdir / target).resolve()
        return gitdir
    except OSError:
        return None


def within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def managed_worktree_root() -> Optional[Path]:
    """The agent worktree root (``agent_worktree.config``), when it exists."""
    try:
        from src.agent_worktree.config import load_config

        root = Path(load_config().worktree_root).expanduser().resolve()
    except Exception:  # noqa: BLE001 - no config means no managed worktrees
        return None
    return root if root.is_dir() else None


def belongs_to_workspace(worktree: Path, workspace: Path,
                         root: Optional[Path] = None) -> bool:
    """Is *worktree* a managed worktree of the repository *workspace* is in?

    *worktree* must sit under the worktree root (*root*, default the
    configured one) and be a linked worktree whose shared ``.git`` is the
    workspace's, or lives inside the workspace.
    """
    root = root if root is not None else managed_worktree_root()
    if root is None or not within(worktree, root) or worktree == root:
        return False
    common = git_common_dir(worktree)
    if common is None or not (worktree / ".git").is_file():
        return False
    owner_repo = common.parent if common.name == ".git" else common
    if within(owner_repo, workspace):
        return True
    workspace_common = git_common_dir(workspace)
    return workspace_common is not None and workspace_common == common


def workspace_worktree_for(path: str, workspace: str) -> Optional[str]:
    """The managed worktree of *workspace*'s repository that contains *path*.

    *path* is an already-resolved absolute path. Returns the worktree's top
    directory, or None when *path* is not inside such a worktree.
    """
    root = managed_worktree_root()
    if root is None:
        return None
    try:
        target = Path(path).resolve()
        base = Path(workspace).expanduser().resolve()
    except (OSError, ValueError):
        return None
    if not within(target, root) or target == root:
        return None
    # The worktree's top is the nearest directory (at or above *path*, below
    # the root) holding a .git pointer file. Walk up; a path that does not
    # exist yet (a file about to be written) still has existing parents.
    candidate = target
    while candidate != root and within(candidate, root):
        if (candidate / ".git").is_file():
            return str(candidate) if belongs_to_workspace(candidate, base, root) else None
        if candidate.parent == candidate:
            break
        candidate = candidate.parent
    return None

