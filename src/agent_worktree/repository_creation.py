"""Bounded clone/init workflows for approved repository roots."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from dulwich.repo import Repo

from src.agent_worktree import repository_sync as sync
from src.agent_worktree.repository_remote import _branch_name, _client


def _fail(code: str, message: str) -> None:
    raise sync.RepositorySyncError(code, message)


def _clone_sync(path: Path, *, source: object, token=None, branch=None, depth=None):
    if path.exists():
        _fail("target_exists", "clone target must not already exist")
    if not isinstance(source, str):
        _fail("invalid_source", "clone source must be a GitHub HTTPS URL")
    url = sync._https_url(source)
    selected_branch = _branch_name(branch) if branch else None
    if depth is not None and (
        isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= 1000
    ):
        _fail("invalid_depth", "clone depth must be an integer from 1 to 1000")
    client, remote_path = _client(url, token)
    repo = None
    complete = False
    try:
        repo = client.clone(
            remote_path,
            str(path),
            mkdir=True,
            bare=False,
            origin="origin",
            checkout=False,
            branch=selected_branch,
            depth=depth,
            filter_spec=None,
            bundle_uri=None,
        )
        head = repo.refs[b"HEAD"]
        sync._validate_tree(repo, head)
        repo.get_worktree().reset_index(config=repo.get_config_stack())
        repo.close()
        repo = None
        validated = sync._validate_path(path)
        result = sync._status_sync(validated)
        complete = True
        return {**result, "action": "clone", "source": url}
    except sync.RepositorySyncError:
        raise
    except Exception:
        _fail("clone_failed", "GitHub clone failed; the incomplete target was removed")
    finally:
        if repo is not None:
            repo.close()
        close = getattr(client, "close", None)
        if callable(close):
            close()
        if path.exists() and not complete:
            shutil.rmtree(path)


def _init_sync(path: Path, *, initial_branch=None):
    branch = _branch_name(initial_branch or "main", field="initial_branch")
    created = not path.exists()
    if created:
        path.mkdir()
    try:
        repo = Repo.init(str(path))
        repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/" + branch.encode())
        repo.close()
        return {
            "ok": True,
            "action": "init",
            "repository": str(path),
            "branch": branch,
            "head": None,
            "unborn": True,
        }
    except Exception:
        if created and path.exists():
            shutil.rmtree(path)
        elif (path / ".git").exists():
            shutil.rmtree(path / ".git")
        _fail("init_failed", "repository could not be initialized safely")


async def execute_creation(action: str, repository: str, token=None, **args) -> Any:
    if action == "clone":
        return await sync.run_new_repository_operation(
            repository,
            lambda path: _clone_sync(
                path,
                source=args.get("source"),
                token=token,
                branch=args.get("branch"),
                depth=args.get("depth"),
            ),
        )
    if action == "init":
        return await sync.run_new_repository_operation(
            repository,
            lambda path: _init_sync(path, initial_branch=args.get("initial_branch")),
            allow_empty_directory=True,
        )
    _fail(
        "unsupported_action", "supported repository creation actions are clone and init"
    )
