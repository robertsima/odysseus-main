"""Bounded Git remote and local-branch operations for approved repositories."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any, Optional

from dulwich.client import get_transport_and_path_from_url
from dulwich.file import FileLocked, GitFile
from dulwich.graph import can_fast_forward
from dulwich.repo import Repo
from dulwich.stash import Stash

from src.agent_worktree import repository_sync as sync
from src.agent_worktree.config import load_config
from src.agent_worktree.push_guard import is_odysseus_repository
from src.agent_worktree.repository_sync import (
    RepositorySyncError,
    _branch_upstream,
    _http_pool,
    _https_url,
    _status,
    _validate_tree,
    checkout_commit,
    pull_repository,
    run_repository_operation,
)
from src.agent_worktree.validation import normalize_branch


def _fail(code: str, message: str) -> None:
    raise RepositorySyncError(code, message)


def _branch_name(value: object, *, field: str = "branch") -> str:
    branch = normalize_branch(value)
    if not branch or branch.startswith("refs/"):
        _fail("invalid_branch", f"{field} must be a plain valid branch name")
    return branch


def _client(url: str, token: Optional[str]):
    return get_transport_and_path_from_url(
        url,
        config=None,
        username="x-access-token" if token else None,
        password=token,
        pool_manager=_http_pool(),
    )


def _fetch_exact(repo: Repo, url: str, remote_ref: bytes, token: Optional[str]):
    client, remote_path = _client(url, token)

    def wants(refs, depth=None):
        if remote_ref not in refs:
            _fail("missing_remote_branch", "the requested remote branch does not exist")
        oid = refs[remote_ref]
        return [] if oid in repo.object_store else [oid]

    try:
        result = client.fetch(remote_path.encode(), repo, determine_wants=wants)
    except RepositorySyncError:
        raise
    except Exception:
        _fail("network_error", "GitHub fetch failed")
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    oid = result.refs.get(remote_ref)
    if not oid or oid not in repo.object_store:
        _fail("fetch_failed", "the requested remote commit was not fetched")
    return oid


def _fetch_sync(path, token=None):
    with Repo(str(path)) as repo:
        _local_ref, branch, remote, merge_ref, raw_url = _branch_upstream(repo)
        url = _https_url(raw_url)
        oid = _fetch_exact(repo, url, merge_ref, token)
        _validate_tree(repo, oid)
        suffix = merge_ref[len(b"refs/heads/") :]
        tracking = b"refs/remotes/" + remote.encode() + b"/" + suffix
        try:
            old = repo.refs[tracking]
        except KeyError:
            old = None
        repo.refs[tracking] = oid
        return {
            "ok": True,
            "action": "fetch",
            "repository": str(path),
            "branch": branch,
            "remote": remote,
            "remote_branch": suffix.decode(),
            "before": old.decode() if old else None,
            "after": oid.decode(),
            "updated": old != oid,
        }


def _expected(value: object, actual: bytes, field: str) -> None:
    if not isinstance(value, str) or value.encode() != actual:
        _fail("stale_approval", f"{field} no longer matches the approved commit")


def _attached_head(repo: Repo):
    raw = repo.refs.read_ref(b"HEAD")
    if not raw or not raw.startswith(b"ref: refs/heads/"):
        _fail("detached_head", "checkout must be on an attached branch")
    ref = raw[5:]
    return ref, ref[len(b"refs/heads/") :].decode(), repo.refs[ref]


def _configured_remotes(repo: Repo):
    cfg = repo.get_config()
    remotes = {}
    for section in cfg.sections():
        if len(section) != 2 or section[0].lower() != b"remote":
            continue
        try:
            url = cfg.get(section, b"url").decode()
        except (KeyError, UnicodeDecodeError):
            continue
        remotes[section[1].decode()] = _https_url(url)
    return remotes


def _select_remote(repo: Repo, preferred: Optional[str] = None):
    remotes = _configured_remotes(repo)
    if preferred:
        if preferred not in remotes:
            _fail("missing_remote", "selected remote is not configured or is unsafe")
        return preferred, remotes[preferred]
    if "origin" in remotes:
        return "origin", remotes["origin"]
    if len(remotes) == 1:
        return next(iter(remotes.items()))
    _fail(
        "ambiguous_remote",
        "repository must have origin or exactly one configured GitHub remote",
    )


def _upstream(repo: Repo, branch: str):
    cfg = repo.get_config()
    key = (b"branch", branch.encode())
    try:
        remote = cfg.get(key, b"remote").decode()
        merge_ref = cfg.get(key, b"merge")
    except (KeyError, UnicodeDecodeError):
        return None
    if not merge_ref.startswith(b"refs/heads/"):
        _fail("invalid_upstream", "upstream must be a branch ref")
    remotes = _configured_remotes(repo)
    if remote not in remotes:
        _fail("missing_remote", "configured upstream remote is unavailable or unsafe")
    return remote, merge_ref, remotes[remote]


def _is_app_remote(url: str) -> bool:
    cfg = load_config()
    path = url.split("github.com/", 1)[-1].rstrip("/")
    if path.casefold().endswith(".git"):
        path = path[:-4]
    return bool(cfg.repo_slug and path.casefold() == cfg.repo_slug.casefold())


def _push_sync(path, token=None, remote_branch=None, expected_head=None):
    with Repo(str(path)) as repo:
        local_ref, branch, local_oid = _attached_head(repo)
        upstream = _upstream(repo, branch)
        if upstream:
            remote, merge_ref, url = upstream
        else:
            if not remote_branch:
                _fail(
                    "remote_branch_required",
                    "first push requires an explicit remote_branch",
                )
            remote, url = _select_remote(repo)
            merge_ref = (
                b"refs/heads/"
                + _branch_name(remote_branch, field="remote_branch").encode()
            )
        if is_odysseus_repository(str(path)) or _is_app_remote(url):
            _fail(
                "use_publish_flow",
                "Odysseus repositories must use request_publish/publish",
            )
        if not _status(repo)["clean"]:
            _fail("dirty_tree", "working tree must be clean before push")
        target_name = (
            _branch_name(remote_branch, field="remote_branch")
            if remote_branch
            else merge_ref[len(b"refs/heads/") :].decode()
        )
        target_ref = b"refs/heads/" + target_name.encode()
        _expected(expected_head, local_oid, "HEAD")
        _validate_tree(repo, local_oid)

        # Fetch the exact target first so ancestry is checked against current
        # remote state, not a stale remote-tracking ref.
        remote_oid = None
        try:
            remote_oid = _fetch_exact(repo, url, target_ref, token)
        except RepositorySyncError as exc:
            if exc.code != "missing_remote_branch":
                raise
        if remote_oid and not can_fast_forward(repo, remote_oid, local_oid):
            _fail("non_fast_forward", "remote branch is not an ancestor of local HEAD")

        client, remote_path = _client(url, token)

        def update_refs(refs):
            observed = refs.get(target_ref)
            if observed != remote_oid:
                _fail("remote_changed", "remote branch changed after validation")
            return {target_ref: local_oid}

        try:
            result = client.send_pack(
                remote_path.encode(),
                update_refs,
                repo.generate_pack_data,
                atomic=True,
            )
        except RepositorySyncError:
            raise
        except Exception:
            _fail("push_failed", "GitHub rejected or interrupted the push")
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        ref_status = getattr(result, "ref_status", None) or {}
        if ref_status.get(target_ref):
            _fail("push_failed", "GitHub rejected the branch update")
        tracking = b"refs/remotes/" + remote.encode() + b"/" + target_name.encode()
        repo.refs[tracking] = local_oid
        return {
            "ok": True,
            "action": "push",
            "repository": str(path),
            "branch": branch,
            "remote": remote,
            "remote_branch": target_name,
            "before": remote_oid.decode() if remote_oid else None,
            "after": local_oid.decode(),
            "updated": remote_oid != local_oid,
            "needs_upstream_action": upstream is None,
        }


def _delete_branch_sync(path, branch, expected_head=None, expected_target=None):
    name = _branch_name(branch)
    target_ref = b"refs/heads/" + name.encode()
    with Repo(str(path)) as repo:
        current_ref, current, head = _attached_head(repo)
        if target_ref == current_ref:
            _fail("current_branch", "the current branch cannot be deleted")
        try:
            target = repo.refs[target_ref]
        except KeyError:
            _fail("missing_branch", "local branch does not exist")
        _expected(expected_head, head, "HEAD")
        _expected(expected_target, target, "target branch")
        if not can_fast_forward(repo, target, head):
            _fail("unmerged_branch", "branch is not fully merged into current HEAD")
        if not repo.refs.remove_if_equals(target_ref, target):
            _fail("stale_branch", "branch changed before it could be deleted")
        return {
            "ok": True,
            "action": "delete_branch",
            "repository": str(path),
            "branch": name,
            "deleted": target.decode(),
            "current_branch": current,
        }


def _switch_sync(path, name):
    branch = _branch_name(name, field="name")
    target_ref = b"refs/heads/" + branch.encode()
    with Repo(str(path)) as repo:
        current_ref, current, before = _attached_head(repo)
        if not _status(repo)["clean"]:
            _fail("dirty_tree", "working tree must be clean before switching branches")
        try:
            target = repo.refs[target_ref]
        except KeyError:
            _fail("missing_branch", "local branch does not exist")
        if target_ref != current_ref:
            checkout_commit(path, repo, before, target, new_symbolic_head=target_ref)
        return {
            "ok": True,
            "action": "switch",
            "repository": str(path),
            "before_branch": current,
            "branch": branch,
            "head": target.decode(),
            "updated": target_ref != current_ref,
        }


def _merge_ref(repo: Repo, remote: str, value: object):
    text = str(value or "").strip()
    if text.startswith(remote + "/"):
        branch = _branch_name(text[len(remote) + 1 :], field="ref")
        return b"refs/remotes/" + remote.encode() + b"/" + branch.encode(), text
    branch = _branch_name(text, field="ref")
    return b"refs/heads/" + branch.encode(), branch


def _merge_sync(path, ref, expected_head=None, expected_target=None):
    with Repo(str(path)) as repo:
        current_ref, current, head = _attached_head(repo)
        if not _status(repo)["clean"]:
            _fail("dirty_tree", "working tree must be clean before merge")
        text = str(ref or "").strip()
        if "/" in text:
            remote_name = text.split("/", 1)[0]
            if remote_name not in _configured_remotes(repo):
                _fail("missing_remote", "remote-tracking ref names an unknown remote")
        else:
            remote_name = ""
        target_ref, label = _merge_ref(repo, remote_name, ref)
        try:
            target = repo.refs[target_ref]
        except KeyError:
            _fail("missing_ref", "merge ref does not exist locally")
        _expected(expected_head, head, "HEAD")
        _expected(expected_target, target, "merge target")
        if not can_fast_forward(repo, head, target):
            _fail(
                "non_fast_forward", "merge target is not a fast-forward of current HEAD"
            )
        if target != head:
            checkout_commit(path, repo, head, target, update_ref=current_ref)
        return {
            "ok": True,
            "action": "merge",
            "repository": str(path),
            "branch": current,
            "ref": label,
            "before": head.decode(),
            "after": target.decode(),
            "updated": target != head,
        }


def _set_upstream_sync(path, remote=None, remote_branch=None):
    branch_name = _branch_name(remote_branch, field="remote_branch")
    with Repo(str(path)) as repo:
        _current_ref, current, _head = _attached_head(repo)
        existing = _upstream(repo, current)
        selected_remote, _url = _select_remote(
            repo, str(remote).strip() if remote else None
        )
        merge_ref = b"refs/heads/" + branch_name.encode()
        if existing:
            old_remote, old_merge, _old_url = existing
            if old_remote == selected_remote and old_merge == merge_ref:
                return {
                    "ok": True,
                    "action": "set_upstream",
                    "repository": str(path),
                    "branch": current,
                    "remote": old_remote,
                    "remote_branch": branch_name,
                    "upstream_configured": False,
                    "reason": "already_configured",
                }
            _fail(
                "upstream_exists",
                "current branch already has an upstream; refusing to replace it",
            )
        tracking = (
            b"refs/remotes/" + selected_remote.encode() + b"/" + branch_name.encode()
        )
        try:
            tracked_oid = repo.refs[tracking]
        except KeyError:
            _fail(
                "missing_tracking_ref",
                "remote branch has not been fetched or pushed locally",
            )
        _validate_tree(repo, tracked_oid)

        config_path = path / ".git" / "config"
        snapshot = config_path.read_bytes()
        cfg = repo.get_config()
        section = (b"branch", current.encode())
        cfg.set(section, b"remote", selected_remote.encode())
        cfg.set(section, b"merge", merge_ref)
        try:
            with GitFile(str(config_path), "wb") as output:
                if config_path.read_bytes() != snapshot:
                    _fail(
                        "config_changed",
                        "repository config changed before upstream could be saved",
                    )
                cfg.write_to_file(output)
        except RepositorySyncError:
            raise
        except FileLocked:
            _fail(
                "config_busy", "repository config is being changed by another process"
            )
        except OSError:
            _fail(
                "config_write_failed", "repository upstream could not be saved safely"
            )
        return {
            "ok": True,
            "action": "set_upstream",
            "repository": str(path),
            "branch": current,
            "remote": selected_remote,
            "remote_branch": branch_name,
            "upstream_configured": True,
            "tracking_head": tracked_oid.decode(),
        }


def _restore_autostash(
    path: Path,
    stash_oid: bytes,
    changed: list[str],
    staged: set[str],
    worktree_snapshots: dict[str, tuple[bytes, int] | None],
    index_entries: dict[bytes, object | None],
) -> None:
    """Restore only originally changed paths, never the stash's whole old tree."""
    with Repo(str(path)) as repo:
        stash = Stash(repo)
        try:
            current = stash[0]
        except (IndexError, FileNotFoundError):
            _fail(
                "stash_missing", "saved local changes are missing; inspect the checkout"
            )
        if current.new_sha != stash_oid:
            _fail(
                "stash_changed",
                "stash state changed during pull; saved changes remain in refs/stash",
            )
        current_dirty = _status(repo)
        unexpectedly_changed = (
            set(current_dirty["staged"]) | set(current_dirty["unstaged"])
        ).intersection(changed)
        if unexpectedly_changed:
            _fail(
                "restore_changed",
                "saved paths changed during pull; local changes remain in refs/stash",
            )
        index_path = path / ".git" / "index"
        index_snapshot = index_path.read_bytes() if index_path.exists() else None
        pulled_snapshots: dict[str, tuple[bytes, int] | None] = {}
        for relative in changed:
            target = path / Path(relative)
            if target.is_file():
                info = target.stat()
                pulled_snapshots[relative] = (
                    target.read_bytes(),
                    stat.S_IMODE(info.st_mode),
                )
            else:
                pulled_snapshots[relative] = None
        try:
            index = repo.open_index(config=repo.get_config())
            for relative in changed:
                target = path / Path(relative)
                snapshot = worktree_snapshots[relative]
                if snapshot is None:
                    if target.exists():
                        if not target.is_file():
                            _fail(
                                "unsafe_worktree",
                                "restore target is not a regular file",
                            )
                        target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(snapshot[0])
                    target.chmod(snapshot[1])
                if relative in staged:
                    raw = os.fsencode(relative)
                    entry = index_entries[raw]
                    if entry is None:
                        if raw in index:
                            del index[raw]
                    else:
                        index[raw] = entry
            index.write()
            stash.drop(0)
        except Exception:  # noqa: BLE001 - stash internals expose no stable exception set
            for relative, snapshot in pulled_snapshots.items():
                target = path / Path(relative)
                if snapshot is None:
                    if target.is_file():
                        target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(snapshot[0])
                    target.chmod(snapshot[1])
            if index_snapshot is None:
                index_path.unlink(missing_ok=True)
            else:
                index_path.write_bytes(index_snapshot)
            _fail(
                "restore_failed",
                "pull completed but saved changes could not be restored; recover refs/stash",
            )


def _pull_with_restore_sync(path: Path, token=None):
    """Fast-forward a dirty checkout while preserving bounded local file changes."""
    with Repo(str(path)) as repo:
        dirty = _status(repo)
        changed = sorted(set(dirty["staged"]) | set(dirty["unstaged"]))
        protected = changed + list(dirty["untracked"])
        if len(protected) > 1000:
            _fail("stash_too_large", "at most 1000 local paths can be preserved")
        total = 0
        staged = set(dirty["staged"])
        index = repo.open_index(config=repo.get_config())
        worktree_snapshots: dict[str, tuple[bytes, int] | None] = {}
        index_entries: dict[bytes, object | None] = {}
        for relative in changed:
            target = path / Path(relative)
            raw = os.fsencode(relative)
            entry = index[raw] if raw in index else None
            if entry is not None and not stat.S_ISREG(entry.mode):
                _fail("unsafe_index", "only regular file changes can be preserved")
            index_entries[raw] = entry
            if target.exists():
                info = target.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                    _fail(
                        "unsafe_worktree",
                        "only regular unlinked changes can be preserved",
                    )
                data = target.read_bytes()
                if Path(relative).name.casefold() == ".gitattributes" and re.search(
                    rb"(?:^|\s)(?:filter|diff|merge)\s*=",
                    data,
                    re.MULTILINE | re.IGNORECASE,
                ):
                    _fail(
                        "unsafe_attributes",
                        "Git attributes that invoke filters or drivers are not supported",
                    )
                total += len(data)
                worktree_snapshots[relative] = (data, stat.S_IMODE(info.st_mode))
            else:
                worktree_snapshots[relative] = None
            if total > 64 * 1024 * 1024:
                _fail("stash_too_large", "saved local changes must be at most 64 MiB")
        if not changed:
            stash_oid = None
        else:
            stash = Stash(repo)
            try:
                stash_oid = stash.push(
                    author=b"Odysseus Autostash <odysseus@localhost>",
                    committer=b"Odysseus Autostash <odysseus@localhost>",
                    message=b"Odysseus pull_with_restore",
                    config=repo.get_config(),
                )
            except Exception:  # noqa: BLE001 - stash internals expose no stable exception set
                _fail("stash_failed", "local changes could not be saved safely")

    try:
        result = sync._pull_sync(
            path,
            token,
            allow_untracked=True,
            protected_paths=protected,
        )
    except RepositorySyncError as exc:
        if stash_oid is None or exc.code == "recovery_required":
            raise
        try:
            _restore_autostash(
                path,
                stash_oid,
                changed,
                staged,
                worktree_snapshots,
                index_entries,
            )
        except RepositorySyncError:
            raise RepositorySyncError(
                "recovery_required",
                "pull stopped and saved changes could not be restored; recover refs/stash",
            ) from None
        raise

    if stash_oid is not None:
        _restore_autostash(
            path,
            stash_oid,
            changed,
            staged,
            worktree_snapshots,
            index_entries,
        )
    final = sync._status_sync(path)
    return {
        **result,
        "dirty": final["dirty"],
        "saved_and_restored": bool(stash_oid),
        "preserved_untracked": len(dirty["untracked"]),
    }


async def execute_remote(action, repository, token=None, **args) -> Any:
    """Execute one allowlisted repository operation; never accepts raw Git args."""
    action = str(action or "").strip().lower()
    if action == "pull":
        return await pull_repository(repository, token=token)
    if action == "pull_with_restore":
        return await run_repository_operation(
            repository, lambda path: _pull_with_restore_sync(path, token)
        )
    if action == "fetch":
        return await run_repository_operation(
            repository, lambda path: _fetch_sync(path, token)
        )
    if action == "push":
        return await run_repository_operation(
            repository,
            lambda path: _push_sync(
                path, token, args.get("remote_branch"), args.get("expected_head")
            ),
        )
    if action == "delete_branch":
        return await run_repository_operation(
            repository,
            lambda path: _delete_branch_sync(
                path,
                args.get("name"),
                args.get("expected_head"),
                args.get("expected_target"),
            ),
        )
    if action == "switch":
        return await run_repository_operation(
            repository, lambda path: _switch_sync(path, args.get("name"))
        )
    if action == "merge":
        return await run_repository_operation(
            repository,
            lambda path: _merge_sync(
                path,
                args.get("ref"),
                args.get("expected_head"),
                args.get("expected_target"),
            ),
        )
    if action == "set_upstream":
        return await run_repository_operation(
            repository,
            lambda path: _set_upstream_sync(
                path, args.get("remote"), args.get("remote_branch")
            ),
        )
    if action in {"reset", "rebase", "force_push", "delete_remote_branch"}:
        _fail("unsupported_action", f"{action} is intentionally unsupported")
    _fail(
        "unsupported_action",
        "supported actions are fetch, pull, pull_with_restore, push, switch, merge, delete_branch, and set_upstream",
    )
