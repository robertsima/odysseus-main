"""Review-bound stash and local history workflows with durable recovery."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from dulwich import porcelain
from dulwich.index import IndexEntry
from dulwich.object_store import iter_tree_contents
from dulwich.objects import Commit
from dulwich.repo import Repo
from dulwich.stash import Stash

from src.agent_worktree import repository_sync as sync
from src.agent_worktree.repository_local import _resolve_ref
from src.agent_worktree.repository_remote import _attached_head, _expected, _merge_ref


def _fail(code: str, message: str) -> None:
    raise sync.RepositorySyncError(code, message)


def _stash_entries(repo: Repo) -> list[dict[str, Any]]:
    rows = []
    for index, entry in enumerate(Stash(repo).stashes()[:100]):
        commit = repo[entry.new_sha]
        base = (
            commit.parents[0] if isinstance(commit, Commit) and commit.parents else None
        )
        rows.append(
            {
                "index": index,
                "id": entry.new_sha.decode(),
                "base": base.decode() if base else None,
                "timestamp": entry.timestamp,
                "message": entry.message.decode("utf-8", "replace")[:500],
            }
        )
    return rows


def _stash_entry(repo: Repo, index: object, expected_target: object):
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 100:
        _fail("invalid_stash", "stash index must be an integer from 0 to 99")
    try:
        entry = Stash(repo)[index]
    except (IndexError, FileNotFoundError):
        _fail("stash_missing", "stash entry does not exist")
    _expected(expected_target, entry.new_sha, "stash")
    commit = repo[entry.new_sha]
    if not isinstance(commit, Commit) or len(commit.parents) < 2:
        _fail("invalid_stash", "stash entry is malformed")
    return entry, commit


def _stash_list_sync(path: Path):
    with Repo(str(path)) as repo:
        return {"repository": str(path), "stashes": _stash_entries(repo)}


def _stash_create_sync(path: Path, message=None):
    if message is not None and (
        not isinstance(message, str) or not message.strip() or len(message) > 500
    ):
        _fail("invalid_message", "stash message must be 1 to 500 characters")
    with Repo(str(path)) as repo:
        dirty = sync._status(repo)
        if not dirty["staged"] and not dirty["unstaged"]:
            _fail("nothing_to_stash", "there are no tracked changes to stash")
        if len(dirty["staged"]) + len(dirty["unstaged"]) > 1000:
            _fail("stash_too_large", "at most 1000 tracked paths can be stashed")
        try:
            oid = Stash(repo).push(
                author=b"Odysseus Stash <odysseus@localhost>",
                committer=b"Odysseus Stash <odysseus@localhost>",
                message=(message or "Odysseus managed stash").strip().encode(),
                config=repo.get_config(),
            )
        except Exception:
            _fail("stash_failed", "tracked changes could not be stashed safely")
        return {
            "repository": str(path),
            "stash": oid.decode(),
            "preserved_untracked": len(dirty["untracked"]),
            "note": "untracked files remain in the working tree",
        }


def _write_index_tree(repo: Repo, tree_id: bytes) -> None:
    index = repo.open_index(config=repo.get_config())
    index.clear()
    for entry in iter_tree_contents(repo.object_store, tree_id):
        if entry.path is None or entry.mode is None or entry.sha is None:
            _fail("invalid_stash", "stash index is malformed")
        index[entry.path] = IndexEntry(0, 0, 0, 0, entry.mode, 0, 0, 0, entry.sha)
    index.write()


def _stash_apply_sync(path: Path, index=0, expected_target=None, *, drop=False):
    with Repo(str(path)) as repo:
        _local_ref, _branch, head = _attached_head(repo)
        dirty = sync._status(repo)
        if dirty["staged"] or dirty["unstaged"]:
            _fail("dirty_tree", "tracked files must be clean before applying a stash")
        entry, commit = _stash_entry(repo, index, expected_target)
        base, index_commit_id = commit.parents[:2]
        if head != base:
            _fail(
                "stash_base_changed",
                "safe stash apply requires the same HEAD where the stash was created; use pull_with_restore for synchronization",
            )
        sync._validate_tree(repo, entry.new_sha)
        index_commit = repo[index_commit_id]
        if not isinstance(index_commit, Commit):
            _fail("invalid_stash", "stash index is malformed")
        sync.checkout_commit(path, repo, base, entry.new_sha)
        try:
            _write_index_tree(repo, index_commit.tree)
            if drop:
                Stash(repo).drop(index)
        except Exception:
            try:
                sync.checkout_commit(path, repo, entry.new_sha, base)
            except sync.RepositorySyncError:
                _fail(
                    "recovery_required",
                    "stash apply stopped; recover the displayed stash",
                )
            _fail(
                "stash_apply_failed",
                "stash apply failed and the clean checkout was restored",
            )
        return {
            "repository": str(path),
            "stash": entry.new_sha.decode(),
            "applied": True,
            "dropped": drop,
            "status": sync._status(repo),
        }


def _stash_drop_sync(path: Path, index=0, expected_target=None):
    with Repo(str(path)) as repo:
        entry, _commit = _stash_entry(repo, index, expected_target)
        Stash(repo).drop(index)
        return {
            "repository": str(path),
            "stash": entry.new_sha.decode(),
            "dropped": True,
        }


def _recovery_ref(action: str) -> bytes:
    return f"refs/odysseus/recovery/{action}/{int(time.time())}-{uuid.uuid4().hex[:12]}".encode()


def _reset_sync(path: Path, ref, expected_head=None, expected_target=None):
    with Repo(str(path)) as repo:
        current_ref, branch, head = _attached_head(repo)
        if not sync._status(repo)["clean"]:
            _fail(
                "dirty_tree",
                "reset refuses a dirty checkout; commit or stash changes first",
            )
        target = _resolve_ref(repo, ref)
        _expected(expected_head, head, "HEAD")
        _expected(expected_target, target, "reset target")
        sync._validate_tree(repo, target)
        recovery = _recovery_ref("reset")
        repo.refs[recovery] = head
        if target != head:
            sync.checkout_commit(path, repo, head, target, update_ref=current_ref)
        return {
            "repository": str(path),
            "action": "reset",
            "branch": branch,
            "before": head.decode(),
            "after": target.decode(),
            "recovery_ref": recovery.decode(),
            "updated": target != head,
        }


def _rebase_sync(path: Path, ref, expected_head=None, expected_target=None):
    with Repo(str(path)) as repo:
        current_ref, branch, head = _attached_head(repo)
        if not sync._status(repo)["clean"]:
            _fail("dirty_tree", "rebase requires a clean checkout")
        text = str(ref or "").strip()
        remote = text.split("/", 1)[0] if "/" in text else ""
        target_ref, label = _merge_ref(repo, remote, text)
        try:
            target = repo.refs[target_ref]
        except KeyError:
            target = _resolve_ref(repo, text)
        _expected(expected_head, head, "HEAD")
        _expected(expected_target, target, "rebase target")
        sync._validate_tree(repo, target)
        recovery = _recovery_ref("rebase")
        repo.refs[recovery] = head
        rebased = False
        materialized = False
        try:
            rewritten = porcelain.rebase(repo, upstream=target)
            rebased = True
            after = repo.refs[current_ref]
            sync._validate_tree(repo, after)
            # Dulwich rewrites the ref but does not materialize the new tree.
            sync.checkout_commit(path, repo, head, after)
            materialized = True
            if not sync._status(repo)["clean"]:
                raise RuntimeError("rebase left a dirty checkout")
        except Exception:
            try:
                if rebased:
                    current = repo.refs[current_ref]
                    if materialized:
                        sync.checkout_commit(
                            path, repo, current, head, update_ref=current_ref
                        )
                    elif not repo.refs.set_if_equals(current_ref, current, head):
                        raise RuntimeError("branch changed during rollback")
                else:
                    porcelain.rebase(repo, upstream=target, abort=True)
            except Exception:
                _fail(
                    "recovery_required",
                    f"rebase stopped and automatic abort failed; recover {recovery.decode()}",
                )
            try:
                restored = repo.refs[current_ref]
            except KeyError:
                restored = None
            if restored != head:
                _fail(
                    "recovery_required",
                    f"rebase stopped; recover {recovery.decode()}",
                )
            _fail(
                "rebase_conflict",
                "rebase conflicted and was aborted; no branch update was kept",
            )
        return {
            "repository": str(path),
            "action": "rebase",
            "branch": branch,
            "ref": label,
            "before": head.decode(),
            "after": after.decode(),
            "rewritten_commits": [oid.decode() for oid in rewritten],
            "recovery_ref": recovery.decode(),
        }


async def execute_history(action: str, repository: str, **args):
    operations = {
        "stash_list": lambda path: _stash_list_sync(path),
        "stash_create": lambda path: _stash_create_sync(path, args.get("message")),
        "stash_apply": lambda path: _stash_apply_sync(
            path, args.get("index", 0), args.get("expected_target")
        ),
        "stash_pop": lambda path: _stash_apply_sync(
            path, args.get("index", 0), args.get("expected_target"), drop=True
        ),
        "stash_drop": lambda path: _stash_drop_sync(
            path, args.get("index", 0), args.get("expected_target")
        ),
        "reset": lambda path: _reset_sync(
            path,
            args.get("ref"),
            args.get("expected_head"),
            args.get("expected_target"),
        ),
        "rebase": lambda path: _rebase_sync(
            path,
            args.get("ref"),
            args.get("expected_head"),
            args.get("expected_target"),
        ),
    }
    operation = operations.get(action)
    if operation is None:
        _fail("unsupported_action", "unsupported history action")
    return await sync.run_repository_operation(repository, operation)
