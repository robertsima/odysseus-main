"""Locked, repository-confined local Git operations without shell or hooks."""

from __future__ import annotations

import io
import os
import re
import stat
import time
from pathlib import Path, PurePosixPath
from typing import Any

from dulwich.index import (
    IndexEntry,
    blob_from_path_and_stat,
    cleanup_mode,
    index_entry_from_stat,
)
from dulwich.object_store import tree_lookup_path
from dulwich.objects import Blob, Commit
from dulwich.patch import write_blob_diff
from dulwich.refs import check_ref_format
from dulwich.repo import Repo

from src.agent_worktree import repository_sync as sync


_ACTIONS = {
    "status",
    "log",
    "diff",
    "branches",
    "remotes",
    "stage",
    "unstage",
    "commit",
    "branch",
    "tag",
}
_MAX_PATHS = 100
_MAX_DIFF = 256_000
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_GLOB_CHARS = frozenset("*?[]{}")


def _fail(code: str, message: str) -> None:
    raise sync.RepositorySyncError(code, message)


def _head(repo: Repo) -> bytes:
    try:
        return repo.refs[b"HEAD"]
    except KeyError:
        _fail("missing_head", "repository has no HEAD commit")


def _commit(repo: Repo, ref: str | bytes | None = None) -> Commit:
    raw = (
        _head(repo)
        if ref in (None, "", b"", "HEAD", b"HEAD")
        else _resolve_ref(repo, ref)
    )
    obj = repo[raw]
    if not isinstance(obj, Commit):
        _fail("invalid_ref", "reference does not resolve to a commit")
    return obj


def _resolve_ref(repo: Repo, ref: str | bytes) -> bytes:
    value = os.fsencode(ref).strip()
    candidates = [value]
    if not value.startswith(b"refs/"):
        # `refs/remotes/` is the namespace `git log upstream/dev` resolves
        # through, and it was missing from this list — so a remote-tracking
        # branch could never be named, even right after a successful fetch. The
        # 2026-09-18 logs show exactly that: the user fetched `upstream/dev`
        # from their own shell, and the agent's `log ref="upstream/dev"` two
        # minutes later still failed with "reference was not found". Order
        # follows git's own: a local branch shadows a same-named remote one.
        candidates = [b"refs/heads/" + value, b"refs/tags/" + value,
                      b"refs/remotes/" + value, value]
    for candidate in candidates:
        try:
            return repo.refs[candidate]
        except KeyError:
            if (
                re.fullmatch(rb"[0-9a-fA-F]{40}", candidate)
                and candidate.lower() in repo.object_store
            ):
                return candidate.lower()
    shown = value.decode("utf-8", "replace")
    if re.fullmatch(rb"[0-9a-fA-F]{40}", value):
        _fail("invalid_ref",
              f"commit {shown} is not in this repository's object store; it has not been fetched. "
              "Fetch the branch that contains it with action='fetch_branch' first.")
    if re.fullmatch(rb"[0-9a-fA-F]{4,39}", value):
        _fail("invalid_ref",
              f"reference {shown!r} was not found. Abbreviated commit ids are not resolved; "
              "pass the full 40-character id, or a branch name.")
    _fail("invalid_ref",
          f"reference {shown!r} was not found as a local branch, tag, or remote-tracking branch "
          f"(refs/remotes/{shown}). If it lives on a remote, fetch it first with "
          "action='fetch_branch'; action='branches' lists what exists locally.")


def _paths(raw: Any) -> list[tuple[str, bytes]]:
    if not isinstance(raw, list) or not raw or len(raw) > _MAX_PATHS:
        _fail("invalid_paths", "paths must contain 1 to 100 exact relative file paths")
    out: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item or item != item.strip():
            _fail("invalid_paths", "every path must be a non-empty string")
        value = item.replace("\\", "/")
        pure = PurePosixPath(value)
        if (
            value in {".", ".."}
            or pure.is_absolute()
            or ".." in pure.parts
            or re.match(r"^[A-Za-z]:", value)
            or value.startswith("//")
            or ".git" in {part.casefold() for part in pure.parts}
            or any(char in value for char in _GLOB_CHARS)
        ):
            _fail(
                "invalid_paths",
                "paths must be exact relative files without globs or metadata traversal",
            )
        sync._validate_raw_parts([os.fsencode(part) for part in pure.parts])
        normalized = pure.as_posix()
        if normalized not in seen:
            out.append((normalized, os.fsencode(normalized)))
            seen.add(normalized)
    return out


def _sensitive(path: Path) -> bool:
    from src.tool_execution import _is_sensitive_path

    return _is_sensitive_path(str(path.resolve(strict=False)), allow_private=False)


def _safe_index(repo: Repo, index) -> None:
    if getattr(index, "is_sparse", lambda: False)():
        _fail("unsupported_sparse_checkout", "sparse indexes are not supported")
    for raw in index:
        rel = os.fsdecode(raw).replace("\\", "/")
        _paths([rel])
        _safe_local_target(Path(repo.path), rel)
        entry = index[raw]
        if not stat.S_ISREG(entry.mode):
            _fail("unsafe_index", "index contains a non-regular file")


def _safe_local_target(
    root: Path, relative: str, *, require_file: bool = False
) -> Path:
    """Validate every existing component without following links or hardlinks."""
    target = root / relative
    if _sensitive(target):
        _fail(
            "sensitive_path",
            "sensitive files cannot be changed by repository operations",
        )
    cursor = root
    for part in PurePosixPath(relative).parts:
        cursor /= part
        if not os.path.lexists(cursor):
            continue
        info = cursor.lstat()
        if stat.S_ISLNK(info.st_mode) or (
            hasattr(os.path, "isjunction") and os.path.isjunction(cursor)
        ):
            _fail("unsafe_worktree", "linked worktree paths are not supported")
        if cursor != target and not stat.S_ISDIR(info.st_mode):
            _fail("unsafe_worktree", "worktree parent is not a directory")
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            _fail("unsafe_worktree", "hard-linked worktree files are not supported")
    if require_file and (
        not target.exists() or not stat.S_ISREG(target.lstat().st_mode)
    ):
        _fail("invalid_path", "path must name an existing regular file")
    return target


def _index(repo: Repo):
    index = repo.open_index(config=repo.get_config())
    _safe_index(repo, index)
    return index


def _tree_entry(repo: Repo, tree: bytes, path: bytes):
    try:
        return tree_lookup_path(repo.__getitem__, tree, path)
    except KeyError:
        return None


def _blob_tuple(
    repo: Repo, path: bytes, entry
) -> tuple[bytes | None, int | None, Blob | None]:
    if entry is None:
        return path, None, None
    mode, sha = entry
    obj = repo[sha]
    return path, mode, obj if isinstance(obj, Blob) else None


class _BoundedDiff(io.BytesIO):
    def __init__(self, maximum: int):
        super().__init__()
        self.maximum = maximum
        self.truncated = False

    def write(self, data: bytes) -> int:
        remaining = self.maximum - self.tell()
        if len(data) > remaining:
            self.truncated = True
        if remaining > 0:
            super().write(data[:remaining])
        return len(data)


def _diff(repo: Repo, staged: bool, maximum: int) -> dict[str, Any]:
    index = _index(repo)
    head = _commit(repo)
    sync._validate_tree(repo, head.id)
    names = set(index)
    # Include tracked paths deleted from the index.
    from dulwich.object_store import iter_tree_contents

    names.update(
        entry.path for entry in iter_tree_contents(repo.object_store, head.tree)
    )
    stream = _BoundedDiff(maximum)
    changed = 0
    total_bytes = 0
    for path in sorted(names):
        relative = os.fsdecode(path).replace("\\", "/")
        _paths([relative])
        _safe_local_target(Path(repo.path), relative)
        old_entry = (
            _tree_entry(repo, head.tree, path)
            if staged
            else ((index[path].mode, index[path].sha) if path in index else None)
        )
        if staged:
            new_entry = (index[path].mode, index[path].sha) if path in index else None
        else:
            target = Path(repo.path) / os.fsdecode(path)
            if target.exists() and target.is_file() and not target.is_symlink():
                st = target.stat()
                if st.st_size > _MAX_FILE_BYTES:
                    _fail("file_too_large", "diff files must be at most 16 MiB")
                blob = blob_from_path_and_stat(os.fsencode(target), st)
                new_entry = (cleanup_mode(st.st_mode), blob.id, blob)
            else:
                new_entry = None
        old_tuple = _blob_tuple(repo, path, old_entry)
        if new_entry is None:
            new_tuple = (path, None, None)
        elif len(new_entry) == 3:
            new_tuple = (path, new_entry[0], new_entry[2])
        else:
            new_tuple = _blob_tuple(repo, path, new_entry)
        sizes = [
            blob.raw_length()
            for blob in (old_tuple[2], new_tuple[2])
            if blob is not None
        ]
        if any(size > _MAX_FILE_BYTES for size in sizes):
            _fail("file_too_large", "diff files must be at most 16 MiB")
        total_bytes += sum(sizes)
        if total_bytes > _MAX_TOTAL_BYTES:
            _fail("diff_too_large", "diff input must be at most 64 MiB")
        old_id = old_tuple[2].id if old_tuple[2] else None
        new_id = new_tuple[2].id if new_tuple[2] else None
        if old_id == new_id and old_tuple[1] == new_tuple[1]:
            continue
        changed += 1
        write_blob_diff(stream, old_tuple, new_tuple)
        if stream.truncated:
            break
    data = stream.getvalue()
    return {
        "staged": staged,
        "changed_files": changed,
        "diff": data.decode("utf-8", errors="replace"),
        "truncated": stream.truncated,
    }


def _identity(repo: Repo, args: dict[str, Any]) -> bytes:
    name = args.get("author_name")
    email = args.get("author_email")
    config = repo.get_config()
    if name is None:
        try:
            name = config.get((b"user",), b"name").decode()
        except KeyError:
            name = None
    if email is None:
        try:
            email = config.get((b"user",), b"email").decode()
        except KeyError:
            email = None
    if (
        not isinstance(name, str)
        or not name.strip()
        or "\n" in name
        or "\r" in name
        or not isinstance(email, str)
        or not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+", email.strip())
    ):
        _fail(
            "missing_identity",
            "commit author name and email must be supplied or configured locally",
        )
    return f"{name.strip()} <{email.strip()}>".encode("utf-8")


def _operate(path: Path, action: str, args: dict[str, Any]) -> dict[str, Any]:
    with Repo(str(path)) as repo:
        try:
            head_id = _head(repo)
        except sync.RepositorySyncError as exc:
            if exc.code != "missing_head":
                raise
            head_id = None
        if head_id is not None:
            sync._validate_tree(repo, head_id)
        if action == "status":
            _index(repo)
            symbolic = repo.refs.read_ref(b"HEAD")
            branch = (
                os.fsdecode(symbolic[len(b"ref: refs/heads/") :])
                if symbolic and symbolic.startswith(b"ref: refs/heads/")
                else None
            )
            return {
                "repository": str(path),
                "head": head_id.decode() if head_id else None,
                "branch": branch,
                "status": sync._status(repo),
                "unborn": head_id is None,
            }
        if action == "log":
            limit = args.get("limit", 20)
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or not 1 <= limit <= 50
            ):
                _fail("invalid_limit", "log limit must be an integer from 1 to 50")
            start = _resolve_ref(repo, args["ref"]) if args.get("ref") else _head(repo)
            rows = []
            for entry in repo.get_walker(include=[start], max_entries=limit):
                commit = entry.commit
                rows.append(
                    {
                        "id": commit.id.decode(),
                        "author": commit.author.decode("utf-8", "replace"),
                        "timestamp": commit.commit_time,
                        "message": (
                            commit.message.decode("utf-8", "replace").splitlines()[0][
                                :500
                            ]
                            if commit.message
                            else ""
                        ),
                    }
                )
            return {"repository": str(path), "commits": rows}
        if action == "diff":
            maximum = args.get("max_output", _MAX_DIFF)
            if (
                isinstance(maximum, bool)
                or not isinstance(maximum, int)
                or not 1_000 <= maximum <= _MAX_DIFF
            ):
                _fail(
                    "invalid_limit",
                    f"max_output must be an integer from 1000 to {_MAX_DIFF}",
                )
            return {
                "repository": str(path),
                **_diff(repo, bool(args.get("staged", False)), maximum),
            }
        if action == "branches":
            current = repo.refs.read_ref(b"HEAD")
            rows = []
            remote_rows = []
            for ref in repo.refs.keys():
                if ref.startswith(b"refs/heads/"):
                    rows.append(
                        {
                            "name": os.fsdecode(ref[len(b"refs/heads/") :]),
                            "target": repo.refs[ref].decode(),
                        }
                    )
                elif ref.startswith(b"refs/remotes/") and len(remote_rows) <= 1000:
                    remote_rows.append(
                        {
                            "name": os.fsdecode(ref[len(b"refs/remotes/") :]),
                            "target": repo.refs[ref].decode(),
                        }
                    )
                if len(rows) > 1000 and len(remote_rows) > 1000:
                    break
            rows.sort(key=lambda row: row["name"])
            remote_rows.sort(key=lambda row: row["name"])
            truncated = len(rows) > 1000 or len(remote_rows) > 1000
            return {
                "repository": str(path),
                "head": head_id.decode() if head_id else None,
                "current": os.fsdecode(current[len(b"ref: refs/heads/") :])
                if current and current.startswith(b"ref: refs/heads/")
                else None,
                "branches": rows[:1000],
                "remote_branches": remote_rows[:1000],
                "truncated": truncated,
            }
        if action == "remotes":
            config = repo.get_config()
            rows = []
            for section in config.sections():
                if len(section) == 2 and section[0].lower() == b"remote":
                    try:
                        url = config.get(section, b"url").decode()
                    except KeyError:
                        continue
                    rows.append(
                        {"name": os.fsdecode(section[1]), "url": sync._https_url(url)}
                    )
            return {
                "repository": str(path),
                "remotes": sorted(rows, key=lambda row: row["name"]),
            }
        if action in {"stage", "unstage"}:
            index = _index(repo)
            selected = _paths(args.get("paths"))
            head = _commit(repo) if head_id is not None else None
            total_bytes = 0
            for relative, raw in selected:
                target = _safe_local_target(path, relative)
                if action == "stage":
                    if target.exists():
                        st = target.lstat()
                        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
                            _fail(
                                "unsafe_worktree",
                                "only regular unlinked files can be staged",
                            )
                        if st.st_size > _MAX_FILE_BYTES:
                            _fail(
                                "file_too_large", "staged files must be at most 16 MiB"
                            )
                        total_bytes += st.st_size
                        if total_bytes > _MAX_TOTAL_BYTES:
                            _fail(
                                "stage_too_large",
                                "staged file content must be at most 64 MiB",
                            )
                        blob = blob_from_path_and_stat(os.fsencode(target), st)
                        repo.object_store.add_object(blob)
                        index[raw] = index_entry_from_stat(st, blob.id)
                    elif raw in index:
                        del index[raw]
                    else:
                        _fail("invalid_path", "path does not exist and is not tracked")
                else:
                    entry = _tree_entry(repo, head.tree, raw) if head else None
                    if entry is None:
                        if raw in index:
                            del index[raw]
                    else:
                        mode, sha = entry
                        if not isinstance(repo[sha], Blob):
                            _fail("invalid_path", "unstage paths must name exact files")
                        index[raw] = IndexEntry(0, 0, 0, 0, mode, 0, 0, 0, sha)
            index.write()
            return {
                "repository": str(path),
                "paths": [p for p, _ in selected],
                "action": action,
            }
        if action == "commit":
            message = args.get("message")
            if (
                not isinstance(message, str)
                or not message.strip()
                or len(message) > 10_000
            ):
                _fail(
                    "invalid_message",
                    "commit message is required and must be at most 10000 characters",
                )
            index = _index(repo)
            parent = head_id
            symbolic = repo.refs.read_ref(b"HEAD")
            if not symbolic or not symbolic.startswith(b"ref: refs/heads/"):
                _fail("detached_head", "commit requires an attached branch")
            tree = index.commit(repo.object_store)
            if parent is None and not len(index):
                _fail("nothing_to_commit", "index has no changes to commit")
            if parent is not None and tree == _commit(repo).tree:
                _fail("nothing_to_commit", "index has no changes to commit")
            ident = _identity(repo, args)
            now = int(time.time())
            commit = Commit()
            commit.tree = tree
            commit.parents = [parent] if parent is not None else []
            commit.author = ident
            commit.committer = ident
            commit.author_time = now
            commit.commit_time = now
            commit.author_timezone = 0
            commit.commit_timezone = 0
            commit.message = message.strip().encode("utf-8")
            repo.object_store.add_object(commit)
            sync._validate_tree(repo, commit.id)
            updated = (
                repo.refs.set_if_equals(symbolic[5:], parent, commit.id)
                if parent is not None
                else repo.refs.add_if_new(symbolic[5:], commit.id)
            )
            if not updated:
                _fail(
                    "changed_during_operation",
                    "branch changed before commit could be recorded",
                )
            return {"repository": str(path), "commit": commit.id.decode()}
        if action in {"branch", "tag"}:
            name = args.get("name")
            if not isinstance(name, str) or not name.strip():
                _fail("invalid_ref", f"{action} name is required")
            prefix = b"refs/heads/" if action == "branch" else b"refs/tags/"
            ref = prefix + name.strip().encode("utf-8")
            if not check_ref_format(ref):
                _fail("invalid_ref", f"invalid {action} name")
            target = _commit(repo, args.get("ref") or "HEAD").id
            if not repo.refs.add_if_new(ref, target):
                _fail("ref_exists", f"{action} already exists")
            return {
                "repository": str(path),
                action: name.strip(),
                "target": target.decode(),
            }
    _fail("unsupported_action", "unsupported local repository action")


async def execute_local(action: str, repository: str, **args: Any) -> dict[str, Any]:
    """Execute one bounded local Git action under the shared repository lock."""
    action = str(action or "").strip().lower()
    if action not in _ACTIONS:
        _fail("unsupported_action", "unsupported local repository action")
    try:
        return await sync.run_repository_operation(
            repository, lambda path: _operate(path, action, args)
        )
    except sync.RepositorySyncError:
        raise
    except Exception:
        raise sync.RepositorySyncError(
            "local_operation_failed",
            "local repository operation failed safely",
        ) from None
