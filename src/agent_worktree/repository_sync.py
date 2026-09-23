"""Narrow, non-shell Git status/fetch/fast-forward service.

This deliberately supports only ordinary physical GitHub HTTPS checkouts.  It
never runs Git, hooks, credential helpers, filters, merges, or submodules.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

import urllib3
from dulwich.client import get_transport_and_path_from_url
from dulwich.diff_tree import tree_changes
from dulwich.ignore import IgnoreFilterManager
from dulwich.index import (
    update_working_tree,
    validate_path_element_default,
    validate_path_element_hfs,
    validate_path_element_ntfs,
)
from dulwich.object_store import iter_commit_contents
from dulwich.objects import S_ISGITLINK, Commit
from dulwich.porcelain import get_tree_changes, get_unstaged_changes
from dulwich.repo import Repo

from src.agent_tools.claude_code_tools import repository_roots
from src.constants import DATA_DIR, PERSONAL_DIR
from src.rag_sensitivity import vault_root

_LOCKS: dict[str, asyncio.Lock] = {}
_SCP_GITHUB = re.compile(
    r"^git@([A-Za-z0-9.-]+):([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?)$"
)


class RepositorySyncError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def as_dict(self):
        return {"ok": False, "code": self.code, "error": str(self)}


def _fail(code, message):
    raise RepositorySyncError(code, message)


_SECRETISH = re.compile(
    r"(?i)(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|x-access-token:[^@\s]+|(?:authorization|token|password)[=:]\s*\S+"
    r"|https?://[^/\s:@]+:[^/\s@]+@)"
)


def _scrub_credential(text: str) -> str:
    """Remove the shared GitHub token (and its basic-auth form) by value.

    _SECRETISH only knows GitHub's own token shapes; an Enterprise token can
    look like anything, so the configured value itself is struck out too.
    """
    import base64

    from src.github_credentials import GITHUB_TOKEN_ENV

    token = os.environ.get(GITHUB_TOKEN_ENV, "").strip()
    if len(token) < 8:
        return text
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return text.replace(basic, "[redacted]").replace(token, "[redacted]")


def describe_remote_failure(exc: BaseException, *, operation: str, url: str = "") -> tuple:
    """Turn a transport exception into (code, message) that says what went wrong.

    Clone and fetch used to catch everything and report only "GitHub clone
    failed" / "GitHub fetch failed". With no cause the agent could only retry
    the identical call — three clones and a fetch in the 2026-09-18 logs, while
    the user's own shell fetched the same remote fine. A 401, a 404, a DNS
    failure and a TLS failure need four different next steps, so name which one
    it was. The detail is bounded and scrubbed: the token travels as HTTP basic
    auth rather than in the URL, but an exception message is still untrusted
    text and is never returned without redaction.
    """
    from dulwich.client import HTTPProxyUnauthorized, HTTPUnauthorized
    from dulwich.errors import GitProtocolError, HangupException, NotGitRepository

    name = type(exc).__name__
    detail = _SECRETISH.sub("[redacted]", _scrub_credential(" ".join(str(exc).split())))[:300]
    if not detail:
        # A bare `assert` carries no message. "AssertionError: no detail" told
        # nobody anything on 2026-09-18; the frame that raised it was the whole
        # diagnosis (urllib3-future's HTTP/2 backend, not GitHub). Name it.
        import traceback

        frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
        if frames:
            last = frames[-1]
            origin = last.filename.replace("\\", "/").split("site-packages/")[-1]
            detail = f"raised at {origin}:{last.lineno} in {last.name}"
    where = f" for {url}" if url else ""
    if isinstance(exc, (HTTPUnauthorized, HTTPProxyUnauthorized)):
        return ("auth_failed",
                f"GitHub {operation} was refused{where}: the credential was missing, expired or "
                "lacks access to this repository. Reconnect GitHub or check the token's repository "
                "access; retrying the same call will fail the same way.")
    if isinstance(exc, NotGitRepository) or "404" in detail or "not found" in detail.lower():
        return ("remote_not_found",
                f"GitHub {operation} failed{where}: the repository or branch was not found. Check "
                "the owner/name spelling, and note a private repository reports as not found "
                f"without a token that can see it. ({name}: {detail})")
    lowered = detail.lower()
    if any(term in lowered for term in ("name or service not known", "getaddrinfo",
                                        "temporary failure in name resolution", "nodename nor servname")):
        return ("network_error", f"GitHub {operation} failed{where}: DNS lookup failed ({name}: {detail})")
    if any(term in lowered for term in ("certificate", "ssl", "tls")):
        return ("network_error", f"GitHub {operation} failed{where}: TLS/certificate error ({name}: {detail})")
    if any(term in lowered for term in ("timed out", "timeout", "connection refused",
                                        "connection reset", "max retries", "newconnectionerror")):
        return ("network_error", f"GitHub {operation} failed{where}: connection problem ({name}: {detail})")
    if isinstance(exc, (GitProtocolError, HangupException)):
        return ("protocol_error", f"GitHub {operation} failed{where}: git protocol error ({name}: {detail})")
    return ("remote_failed", f"GitHub {operation} failed{where} ({name}: {detail or 'no detail'})")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def git_repository_roots() -> tuple:
    """Where `manage_git` may operate: the configured roots plus the harness's own
    worktree root.

    The roots are borrowed from the Claude Code delegation setting
    (`claude_code_repository_roots`). Its built-in default covers
    /app/data/agent_worktrees, but saving that setting for Claude Code's sake
    REPLACES the default, and the harness's worktree root silently dropped out.
    The 2026-09-18 logs show the result: `manage_git status` on a worktree that
    `manage_agent_worktree` had just created was refused as "outside configured
    repository roots", four calls in a row. The worktree root is one the harness
    itself creates and writes, so admitting it widens nothing; every mutating
    action is still behind `manage_git`'s own approval gate.
    """
    roots = list(repository_roots())
    try:
        from src.agent_worktree.config import load_config

        worktree_root = Path(load_config().worktree_root)
    except Exception:
        worktree_root = None
    if worktree_root is not None:
        resolved = worktree_root.resolve(strict=False)
        if not any(resolved == root.resolve(strict=False) for root in roots):
            roots.append(worktree_root)
    return tuple(roots)


def _active_workspace() -> Path | None:
    """The bound workspace, re-vetted, or None.

    Headless workers get one (headless_agent → vet_workspace) and their file
    tools are confined to it, but manage_git consulted only the configured
    roots, so `status` on the very checkout a worker was reading was refused
    as outside the roots. The workspace is already the agent's working area;
    admitting it for Git widens nothing the file tools do not already reach.
    """
    try:
        from src.tool_execution import get_active_workspace, vet_workspace
    except ImportError:
        return None
    vetted = vet_workspace(get_active_workspace() or "")
    return Path(vetted) if vetted else None


def workspace_repository() -> Path | None:
    """The Git checkout the active workspace is in: its top level, or None.

    A workspace is often a subfolder of a repository, so walk up to the
    nearest .git. That top level is admitted as that one repository only
    (see _validate_path), not as a root: a workspace inside a home-directory
    dotfiles checkout must not make every repository under $HOME operable.
    The top level has to pass the same bind-time vetting as a workspace
    (not sensitive, not app state, not a filesystem root).
    """
    workspace = _active_workspace()
    if workspace is None:
        return None
    from src.tool_execution import vet_workspace

    for candidate in (workspace, *workspace.parents):
        if (candidate / ".git").exists() or (candidate / ".git").is_symlink():
            return candidate if vet_workspace(str(candidate)) == str(candidate) else None
    return None


def _operating_roots() -> tuple:
    """git_repository_roots() plus this turn's workspace.

    Kept out of git_repository_roots() itself: that one is context-free and
    cached by the file tools (tool_execution._repository_data_subdirs), where one
    run's workspace must not leak into another's readable roots.
    """
    roots = list(git_repository_roots())
    workspace = _active_workspace()
    if workspace is not None and not any(
        workspace == Path(root).resolve(strict=False) for root in roots
    ):
        roots.append(workspace)
    return tuple(roots)


def _validate_path(raw) -> Path:
    if not isinstance(raw, (str, os.PathLike)) or not str(raw).strip():
        _fail("invalid_path", "repository is required")
    given = Path(raw).expanduser()
    if not given.is_absolute():
        _fail("invalid_path", "repository must be an absolute path")
    absolute = Path(os.path.abspath(given))
    roots = tuple(r.resolve(strict=False) for r in _operating_roots())
    lexical_root = next((root for root in roots if _inside(absolute, root)), None)
    if lexical_root is None and absolute == workspace_repository():
        # The checkout enclosing the workspace, admitted as itself only.
        lexical_root = absolute
    if lexical_root is None:
        # Name the roots. "Outside configured repository roots" alone sent the
        # model round the same four calls; the list tells it where to look.
        _fail("outside_roots",
              "repository is outside configured repository roots ("
              + ", ".join(str(root) for root in roots) + ")")
    cursor = lexical_root
    for part in absolute.relative_to(lexical_root).parts:
        cursor /= part
        if cursor.is_symlink() or (
            hasattr(os.path, "isjunction") and os.path.isjunction(cursor)
        ):
            _fail(
                "symlink_path", "repository paths may not contain symlinks or junctions"
            )
    try:
        resolved = absolute.resolve(strict=True)
    except (FileNotFoundError, OSError):
        _fail("invalid_path", "repository path does not exist or cannot be inspected")
    if not _inside(resolved, lexical_root):
        _fail("outside_roots", "repository resolves outside its configured root")
    personal = Path(PERSONAL_DIR).resolve(strict=False)
    configured_vault = Path(vault_root()).expanduser().resolve(strict=False)
    if (
        resolved == Path(DATA_DIR).resolve(strict=False)
        or _inside(resolved, personal)
        or resolved == configured_vault
        or _inside(resolved, configured_vault)
    ):
        _fail("private_path", "data, vault, and private paths cannot be synchronized")
    gitdir = resolved / ".git"
    if not gitdir.is_dir():
        enclosing = workspace_repository()
        hint = (
            f"; the workspace's checkout is {enclosing}"
            if enclosing is not None and _inside(resolved, enclosing) and resolved != enclosing
            else ""
        )
        _fail(
            "unsupported_checkout",
            "only physical checkouts with a .git directory are supported" + hint,
        )
    if gitdir.is_symlink() or (
        hasattr(os.path, "isjunction") and os.path.isjunction(gitdir)
    ):
        _fail("unsafe_metadata", "linked Git metadata is not supported")
    for item in (
        gitdir / "config",
        gitdir / "HEAD",
        gitdir / "index",
        gitdir / "refs",
        gitdir / "packed-refs",
        gitdir / "objects",
    ):
        if item.is_symlink():
            _fail("unsafe_metadata", "symlinked Git metadata is not supported")
    for base, dirs, files in os.walk(gitdir, followlinks=False):
        for name in [*dirs, *files]:
            item = Path(base) / name
            if item.is_symlink() or (
                hasattr(os.path, "isjunction") and os.path.isjunction(item)
            ):
                _fail(
                    "unsafe_metadata",
                    "symlinked or junction Git metadata is not supported",
                )
            if (
                item.is_file()
                and not _inside(item, gitdir / "objects")
                and item.stat().st_nlink > 1
            ):
                _fail(
                    "unsafe_metadata",
                    "hard-linked mutable Git metadata is not supported",
                )
    if (gitdir / "objects" / "info" / "alternates").exists():
        _fail("unsafe_metadata", "Git object alternates are not supported")
    if (gitdir / "commondir").exists() or (gitdir / "config.worktree").exists():
        _fail("unsafe_metadata", "shared or redirected Git metadata is not supported")
    if (gitdir / "info" / "sparse-checkout").exists():
        _fail("unsafe_metadata", "sparse checkout is not supported")
    cfg = (gitdir / "config").read_text("utf-8", errors="replace")
    lowered = cfg.casefold()
    if (
        re.search(r"\[\s*include(?:if)?\b", lowered)
        or "hookspath" in lowered
        or re.search(r"\[\s*filter\s", lowered)
        or re.search(
            r"^\s*(?:worktree|sparsecheckout(?:cone)?|sparseindex)\s*=",
            lowered,
            re.MULTILINE,
        )
    ):
        _fail(
            "unsafe_config",
            "includes, alternate worktrees, sparse checkout, hooks, and filters are not supported",
        )
    return resolved


def _validate_new_path(raw, *, allow_empty_directory: bool = False) -> Path:
    """Validate a not-yet-repository target under an approved repository root."""
    if not isinstance(raw, (str, os.PathLike)) or not str(raw).strip():
        _fail("invalid_path", "repository target is required")
    given = Path(raw).expanduser()
    if not given.is_absolute():
        _fail("invalid_path", "repository target must be an absolute path")
    absolute = Path(os.path.abspath(given))
    roots = tuple(r.resolve(strict=False) for r in _operating_roots())
    lexical_root = next((root for root in roots if _inside(absolute, root)), None)
    if lexical_root is None or absolute == lexical_root:
        _fail(
            "outside_roots",
            "repository target must be below a configured repository root",
        )
    personal = Path(PERSONAL_DIR).resolve(strict=False)
    configured_vault = Path(vault_root()).expanduser().resolve(strict=False)
    if (
        absolute == Path(DATA_DIR).resolve(strict=False)
        or _inside(absolute, personal)
        or absolute == configured_vault
        or _inside(absolute, configured_vault)
    ):
        _fail(
            "private_path", "data, vault, and private paths cannot contain repositories"
        )
    relative = absolute.relative_to(lexical_root)
    _validate_raw_parts([os.fsencode(part) for part in relative.parts])
    cursor = lexical_root
    for part in relative.parts:
        cursor /= part
        if cursor.exists() and (
            cursor.is_symlink()
            or (hasattr(os.path, "isjunction") and os.path.isjunction(cursor))
        ):
            _fail(
                "symlink_path", "repository paths may not contain symlinks or junctions"
            )
    if absolute.exists():
        if (
            not allow_empty_directory
            or not absolute.is_dir()
            or any(absolute.iterdir())
        ):
            _fail(
                "target_exists",
                "repository target must not exist or must be an empty directory",
            )
    elif not absolute.parent.is_dir():
        _fail("invalid_path", "repository target parent directory does not exist")
    resolved_parent = absolute.parent.resolve(strict=True)
    if not _inside(resolved_parent, lexical_root):
        _fail("outside_roots", "repository target resolves outside its configured root")
    return absolute


def _branch_upstream(repo: Repo):
    raw = repo.refs.read_ref(b"HEAD")
    if not raw or not raw.startswith(b"ref: refs/heads/"):
        _fail("detached_head", "checkout must be on an attached branch")
    local_ref = raw[5:]
    branch = local_ref[len(b"refs/heads/") :]
    cfg = repo.get_config()
    try:
        remote = cfg.get((b"branch", branch), b"remote")
        merge_ref = cfg.get((b"branch", branch), b"merge")
        remote_url = cfg.get((b"remote", remote), b"url").decode()
    except KeyError:
        _fail("missing_upstream", "current branch has no configured upstream")
    if not merge_ref.startswith(b"refs/heads/"):
        _fail("invalid_upstream", "upstream must be a branch ref")
    return local_ref, branch.decode(), remote.decode(), merge_ref, remote_url


def _github_hosts() -> set:
    """github.com, plus the Enterprise host GITHUB_HOST names (if valid)."""
    from src.github_credentials import github_git_host

    return {"github.com", github_git_host() or "github.com"}


def _https_url(raw: str) -> str:
    hosts = _github_hosts()
    m = _SCP_GITHUB.fullmatch(raw.strip())
    if m and m.group(1).casefold() in hosts:
        raw = f"https://{m.group(1).casefold()}/{m.group(2)}"
    p = urlparse(raw)
    host = (p.hostname or "").casefold()
    if (
        p.scheme != "https"
        or host not in hosts
        or p.username
        or p.password
        or p.query
        or p.fragment
        or p.port not in (None, 443)
    ):
        _fail(
            "unsupported_remote",
            "only credential-free https:// remotes on "
            + " or ".join(sorted(hosts)) + " are supported",
        )
    if not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", p.path):
        _fail("unsupported_remote", "GitHub remote path must be owner/repository")
    owner, repository = p.path.strip("/").split("/", 1)
    repository = repository[:-4] if repository.endswith(".git") else repository
    if owner in {".", ".."} or repository in {".", ".."}:
        _fail(
            "unsupported_remote", "GitHub owner and repository names must be explicit"
        )
    return urlunparse(("https", host, p.path, "", "", ""))


def _transport(url: str, token=None):
    """The dulwich client for a validated remote URL.

    Every clone, fetch, pull and push builds its client here, so this is where
    the credential is bound to a host: the token rides as in-process HTTP basic
    auth (never argv, never the URL, so never .git/config), and only when the
    URL's host is the one it was issued for. A github.com checkout on an
    Enterprise install -- or the reverse -- goes anonymous.
    """
    from src.github_credentials import token_for_git_host

    token = token_for_git_host(urlparse(url).hostname, token)
    return get_transport_and_path_from_url(
        url,
        config=None,
        username="x-access-token" if token else None,
        password=token,
        pool_manager=_http_pool(),
    )


def _status(repo: Repo):
    index = repo.open_index(config=repo.get_config())
    if getattr(index, "is_sparse", lambda: False)():
        _fail("unsupported_sparse_checkout", "sparse indexes are not supported")
    if len(index) > 100_000:
        _fail("worktree_too_large", "repository index is too large to inspect safely")
    tracked = {os.fsdecode(path).replace("/", os.sep) for path in index}
    for raw in index:
        relative = os.fsdecode(raw).replace("/", os.sep)
        parts = raw.replace(b"\\", b"/").split(b"/")
        _validate_raw_parts(parts)
        target = Path(repo.path) / relative
        cursor = Path(repo.path)
        for part in Path(relative).parts[:-1]:
            cursor /= part
            if cursor.exists() and (
                cursor.is_symlink()
                or (hasattr(os.path, "isjunction") and os.path.isjunction(cursor))
            ):
                _fail(
                    "unsafe_worktree",
                    "tracked paths may not traverse linked directories",
                )
        if target.exists() and (
            target.is_symlink()
            or (hasattr(os.path, "isjunction") and os.path.isjunction(target))
            or (target.is_file() and target.stat().st_nlink > 1)
        ):
            _fail("unsafe_worktree", "tracked worktree paths may not be linked")
    staged_map = get_tree_changes(repo, index)
    staged = sorted(os.fsdecode(p) for values in staged_map.values() for p in values)
    unstaged = sorted(
        os.fsdecode(p) for p in get_unstaged_changes(index, repo.path, None)
    )
    untracked = []
    # IgnoreFilterManager lazily reads repository .gitignore files. Validate
    # every possible input before constructing it so ignores cannot be links
    # to content outside the checkout.
    ignore_count = 0
    for base, dirs, files in os.walk(repo.path, followlinks=False):
        for name in dirs:
            candidate = Path(base) / name
            if name != ".git" and (
                candidate.is_symlink()
                or (hasattr(os.path, "isjunction") and os.path.isjunction(candidate))
            ):
                _fail("unsafe_worktree", "worktree directories may not be linked")
        dirs[:] = [name for name in dirs if name != ".git"]
        if ".gitignore" in files:
            ignore_count += 1
            ignore_file = Path(base) / ".gitignore"
            info = ignore_file.lstat()
            if (
                ignore_count > 1000
                or info.st_size > 1024 * 1024
                or info.st_nlink > 1
                or stat.S_ISLNK(info.st_mode)
            ):
                _fail(
                    "unsafe_ignore",
                    "repository ignore files must be bounded regular files",
                )
    ignores = IgnoreFilterManager(
        repo.path, global_filters=[], ignorecase=os.name == "nt"
    )
    visited = 0
    for base, dirs, files in os.walk(repo.path, followlinks=False):
        for name in dirs:
            candidate = Path(base) / name
            if name != ".git" and (
                candidate.is_symlink()
                or (hasattr(os.path, "isjunction") and os.path.isjunction(candidate))
            ):
                _fail("unsafe_worktree", "worktree directories may not be linked")
        kept_dirs = []
        for name in dirs:
            if name == ".git":
                continue
            relative_dir = (
                os.path.relpath(os.path.join(base, name), repo.path).replace(
                    os.sep, "/"
                )
                + "/"
            )
            if not ignores.is_ignored(relative_dir):
                kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in files:
            relative = os.path.relpath(os.path.join(base, name), repo.path)
            visited += 1
            if visited > 100_000:
                _fail(
                    "worktree_too_large", "working tree is too large to inspect safely"
                )
            portable = relative.replace(os.sep, "/")
            if relative not in tracked and not ignores.is_ignored(portable):
                untracked.append(portable)
            if len(untracked) > 10_000:
                _fail(
                    "worktree_too_large",
                    "working tree has too many untracked files to inspect safely",
                )
    untracked.sort()
    return {
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "clean": not (staged or unstaged or untracked),
    }


def _validate_tree(repo: Repo, commit_id: bytes) -> None:
    seen = set()
    for entry in iter_commit_contents(repo.object_store, commit_id):
        parts = entry.path.replace(b"\\", b"/").split(b"/")
        _validate_raw_parts(parts)
        portable = "/".join(
            unicodedata.normalize("NFC", os.fsdecode(p)).casefold().rstrip(". ")
            for p in parts
        )
        if portable in seen:
            _fail("unsafe_tree_path", "repository contains colliding portable paths")
        seen.add(portable)
        if S_ISGITLINK(entry.mode):
            _fail(
                "unsupported_submodule",
                "repositories containing submodules are not supported",
            )
        if stat.S_ISLNK(entry.mode):
            _fail("unsafe_tree", "repositories containing symlinks are not supported")
        if entry.path == b".gitattributes" or entry.path.endswith(b"/.gitattributes"):
            data = repo[entry.sha].data
            if re.search(
                rb"(?:^|\s)(?:filter|diff|merge)\s*=",
                data,
                re.MULTILINE | re.IGNORECASE,
            ):
                _fail(
                    "unsafe_attributes",
                    "Git attributes that invoke filters or drivers are not supported",
                )


def _status_sync(path):
    path = _validate_path(path)
    with Repo(str(path)) as repo:
        raw_head = repo.refs.read_ref(b"HEAD")
        if not raw_head or not raw_head.startswith(b"ref: refs/heads/"):
            _fail("detached_head", "checkout must be on an attached branch")
        local_ref = raw_head[5:]
        branch = local_ref[len(b"refs/heads/") :].decode()
        try:
            head = repo.refs[local_ref]
        except KeyError:
            head = None
        if head is not None:
            _validate_tree(repo, head)
        result = {
            "ok": True,
            "repository": str(path),
            "branch": branch,
            "remote": None,
            "remote_url": None,
            "upstream": None,
            "head": head.decode() if head else None,
            "dirty": _status(repo),
            "unborn": head is None,
        }
        try:
            _, _, remote, merge_ref, remote_url = _branch_upstream(repo)
            result.update(
                remote=remote,
                remote_url=_https_url(remote_url),
                upstream=merge_ref.decode(),
            )
        except RepositorySyncError as exc:
            if exc.code != "missing_upstream":
                raise
        return result


async def list_repositories():
    def scan():
        out = []
        seen = set()
        # Same roots the path checks accept, or discovery would list fewer
        # repositories than the tool can actually operate on.
        enclosing = workspace_repository()
        scans = [
            [root, *(p for p in root.iterdir() if p.is_dir())] if root.is_dir() else []
            for root in map(Path, _operating_roots())
        ]
        if enclosing is not None:
            scans.append([enclosing])
        for candidates in scans:
            for candidate in candidates[: 100 - len(out)]:
                marker = candidate / ".git"
                if not marker.exists() or candidate in seen:
                    continue
                seen.add(candidate)
                try:
                    out.append(_status_sync(candidate))
                except RepositorySyncError as exc:
                    out.append({"repository": str(candidate), **exc.as_dict()})
                except Exception:
                    out.append(
                        {
                            "repository": str(candidate),
                            "ok": False,
                            "code": "invalid_repository",
                            "error": "repository could not be inspected safely",
                        }
                    )
        return out[:100]

    return await asyncio.to_thread(scan)


async def repository_status(repository):
    try:
        return await asyncio.to_thread(_status_sync, repository)
    except RepositorySyncError:
        raise
    except Exception:
        raise RepositorySyncError(
            "invalid_repository", "repository could not be inspected safely"
        ) from None


async def run_repository_operation(repository, callback: Callable[[Path], Any]) -> Any:
    """Run one validated repository operation under its canonical lock.

    Cancellation is delivered only after the non-cancellable worker thread
    exits, so a second operation can never overlap a cancelled caller's work.
    """
    try:
        path = await asyncio.to_thread(_validate_path, repository)
    except RepositorySyncError:
        raise
    except Exception:
        raise RepositorySyncError(
            "invalid_repository", "repository could not be inspected safely"
        ) from None
    lock = _LOCKS.setdefault(os.path.normcase(str(path)), asyncio.Lock())
    async with lock:
        worker = asyncio.create_task(asyncio.to_thread(callback, path))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            try:
                await worker
            finally:
                raise


async def run_new_repository_operation(
    repository, callback: Callable[[Path], Any], *, allow_empty_directory: bool = False
) -> Any:
    """Serialize creation at a validated target that does not yet contain Git metadata."""
    try:
        path = await asyncio.to_thread(
            _validate_new_path,
            repository,
            allow_empty_directory=allow_empty_directory,
        )
    except RepositorySyncError:
        raise
    except Exception:
        raise RepositorySyncError(
            "invalid_repository", "repository target could not be inspected safely"
        ) from None
    lock = _LOCKS.setdefault(os.path.normcase(str(path)), asyncio.Lock())
    async with lock:
        worker = asyncio.create_task(asyncio.to_thread(callback, path))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            try:
                await worker
            finally:
                raise


class _NoRedirectPool(urllib3.PoolManager):
    """HTTP pool that never follows redirects (especially with credentials)."""

    def request(self, method, url, *args, **kwargs):
        kwargs["redirect"] = False
        return super().request(method, url, *args, **kwargs)


def _http1_only() -> dict:
    """Pool options that pin git traffic to HTTP/1.1, when that needs saying.

    `caldav` depends on `niquests`, which depends on `urllib3-future` — a fork
    that installs itself AS `urllib3`, so dulwich (which requires real urllib3)
    silently runs on it instead. The fork negotiates HTTP/2 with GitHub, and
    streaming the smart-HTTP ref advertisement then dies inside the fork's own
    backend (`urllib3/backend/hface.py`: `assert self.sock is not None`). Every
    clone and fetch failed that way — reproduced 3/3 against a public repo —
    while the user's shell `git fetch` worked, because git uses libcurl. Git's
    smart-HTTP protocol is what dulwich is built and tested against on
    HTTP/1.1, so pin it there. Stock urllib3 has no `HttpVersion` and needs
    nothing.
    """
    try:
        from urllib3 import HttpVersion
    except ImportError:
        return {}
    return {"disabled_svn": {HttpVersion.h2, HttpVersion.h3}}


def _http_pool():
    return _NoRedirectPool(
        timeout=urllib3.Timeout(connect=10.0, read=60.0),
        retries=urllib3.Retry(
            total=0, connect=0, read=0, redirect=0, raise_on_redirect=True
        ),
        cert_reqs="CERT_REQUIRED",
        **_http1_only(),
    )


def _safe_changes(path: Path, changes) -> None:
    """Reject writes through existing parent links immediately before checkout."""
    for change in changes:
        for side in (change.old, change.new):
            if side is None:
                continue
            rel = Path(os.fsdecode(side.path))
            if rel.is_absolute() or ".." in rel.parts or ".git" in rel.parts:
                _fail("unsafe_tree_path", "upstream contains an unsafe path")
            raw_parts = side.path.replace(b"\\", b"/").split(b"/")
            _validate_raw_parts(raw_parts)
            cursor = path
            for part in rel.parts[:-1]:
                cursor /= part
                if cursor.exists() and (
                    cursor.is_symlink()
                    or (hasattr(os.path, "isjunction") and os.path.isjunction(cursor))
                ):
                    _fail(
                        "unsafe_worktree", "working tree contains a linked parent path"
                    )
            target = path / rel
            if change.old is None and side is change.new and os.path.lexists(target):
                _fail(
                    "untracked_collision",
                    "upstream path collides with an existing untracked path",
                )
            if os.path.lexists(target) and (
                target.is_symlink()
                or (hasattr(os.path, "isjunction") and os.path.isjunction(target))
                or (target.is_file() and target.stat().st_nlink > 1)
            ):
                _fail("unsafe_worktree", "working tree contains a linked update target")


def _validate_raw_parts(raw_parts) -> None:
    if not raw_parts or any(
        not part
        or not validate_path_element_default(part)
        or not validate_path_element_hfs(part)
        or not validate_path_element_ntfs(part)
        or os.fsdecode(part).endswith((".", " "))
        for part in raw_parts
    ):
        _fail("unsafe_tree_path", "repository contains a platform-unsafe path")


def checkout_commit(
    path: Path,
    repo: Repo,
    old_id: bytes,
    new_id: bytes,
    *,
    update_ref: bytes | None = None,
    new_symbolic_head: bytes | None = None,
) -> None:
    """Safely materialize a commit, with bounded rollback and CAS ref update."""
    old_commit, new_commit = repo[old_id], repo[new_id]
    if not isinstance(old_commit, Commit) or not isinstance(new_commit, Commit):
        _fail("invalid_commit", "checkout targets must be commits")
    _validate_tree(repo, new_id)
    changes = list(tree_changes(repo.object_store, old_commit.tree, new_commit.tree))
    if len(changes) > 1000:
        _fail(
            "update_too_large",
            "update changes too many paths for a transactional checkout",
        )
    _safe_changes(path, changes)
    snapshots, expected, total = {}, {}, 0
    expected_total = 0
    for change in changes:
        for side in (change.old, change.new):
            if side is None:
                continue
            target = path / os.fsdecode(side.path)
            if (
                target not in expected
                and change.new is not None
                and side.path == change.new.path
                and stat.S_ISREG(change.new.mode)
            ):
                expected_total += repo[change.new.sha].raw_length()
                if expected_total > 64 * 1024 * 1024:
                    _fail(
                        "update_too_large",
                        "update content is too large for a transactional checkout",
                    )
                expected[target] = repo[change.new.sha].data
            if target in snapshots:
                continue
            if target.is_file():
                info = target.stat()
                if total + info.st_size > 64 * 1024 * 1024:
                    _fail(
                        "update_too_large",
                        "update is too large for a transactional checkout",
                    )
                data = target.read_bytes()
                total += len(data)
                if len(snapshots) >= 1000 or total > 64 * 1024 * 1024:
                    _fail(
                        "update_too_large",
                        "update is too large for a transactional checkout",
                    )
                snapshots[target] = (
                    data,
                    stat.S_IMODE(info.st_mode),
                    info.st_atime_ns,
                    info.st_mtime_ns,
                )
            else:
                snapshots[target] = None
    index_path = path / ".git" / "index"
    index_snapshot = index_path.read_bytes() if index_path.exists() else None
    original_head = repo.refs.read_ref(b"HEAD")
    if new_symbolic_head is not None:
        try:
            if repo.refs[new_symbolic_head] != new_id:
                _fail("changed_during_update", "target branch changed before checkout")
        except KeyError:
            _fail("changed_during_update", "target branch disappeared before checkout")
    if update_ref is not None and not repo.refs.set_if_equals(
        update_ref, old_id, old_id
    ):
        _fail("changed_during_update", "branch changed during checkout")
    try:
        update_working_tree(
            repo,
            old_commit.tree,
            new_commit.tree,
            change_iterator=iter(changes),
            blob_normalizer=None,
            allow_overwrite_modified=False,
            config=repo.get_config(),
        )
        if update_ref is not None and not repo.refs.set_if_equals(
            update_ref, old_id, new_id
        ):
            _fail("changed_during_update", "branch changed during checkout")
        if new_symbolic_head is not None:
            if (
                repo.refs.read_ref(b"HEAD") != original_head
                or repo.refs[new_symbolic_head] != new_id
            ):
                _fail("changed_during_update", "branch state changed during checkout")
            repo.refs.set_symbolic_ref(b"HEAD", new_symbolic_head)
    except Exception:
        current_index = index_path.read_bytes() if index_path.exists() else None
        refs_unchanged = repo.refs.read_ref(b"HEAD") == original_head
        if update_ref is not None:
            try:
                current_update_ref = repo.refs[update_ref]
            except KeyError:
                current_update_ref = None
            refs_unchanged = refs_unchanged and current_update_ref in {old_id, new_id}
        safe_to_restore = (
            all(
                (target.read_bytes() if target.is_file() else None)
                in (snapshot[0] if snapshot is not None else None, expected.get(target))
                for target, snapshot in snapshots.items()
            )
            and current_index == index_snapshot
            and refs_unchanged
        )
        if not safe_to_restore:
            recovery = path / ".git" / "odysseus-recovery" / uuid.uuid4().hex
            recovery.mkdir(parents=True)
            manifest = []
            for target, snapshot in snapshots.items():
                relative = target.relative_to(path)
                manifest.append(
                    {"path": relative.as_posix(), "existed": snapshot is not None}
                )
                if snapshot is not None:
                    backup = recovery / "files" / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    backup.write_bytes(snapshot[0])
            if index_snapshot is not None:
                (recovery / "index").write_bytes(index_snapshot)
            (recovery / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            raise RepositorySyncError(
                "recovery_required",
                f"checkout stopped after concurrent or unexpected changes; recovery backup: {recovery}",
            ) from None
        for target, snapshot in snapshots.items():
            if snapshot is None:
                if target.is_file():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(snapshot[0])
                os.chmod(target, snapshot[1])
                os.utime(target, ns=(snapshot[2], snapshot[3]))
        if index_snapshot is None:
            if index_path.exists():
                index_path.unlink()
        else:
            index_path.write_bytes(index_snapshot)
        if update_ref is not None:
            repo.refs.set_if_equals(update_ref, new_id, old_id)
        raise RepositorySyncError(
            "checkout_failed", "checkout failed; prior checkout was restored"
        ) from None


def _pull_sync(repository, token=None, *, allow_untracked=False, protected_paths=None):
    path = _validate_path(repository)
    with Repo(str(path)) as repo:
        local_ref, branch, remote, merge_ref, raw_url = _branch_upstream(repo)
        url = _https_url(raw_url)
        dirty = _status(repo)
        if (
            dirty["staged"]
            or dirty["unstaged"]
            or (dirty["untracked"] and not allow_untracked)
        ):
            _fail("dirty_tree", "working tree must be completely clean before pull")
        before = repo.refs[local_ref]
        _validate_tree(repo, before)
        client, remote_path = _transport(url, token)

        def wants(refs, depth=None):
            if merge_ref not in refs:
                _fail(
                    "missing_remote_branch", "configured upstream branch was not found"
                )
            return [] if refs[merge_ref] in repo.object_store else [refs[merge_ref]]

        try:
            result = client.fetch(remote_path.encode(), repo, determine_wants=wants)
        except RepositorySyncError:
            raise
        except Exception:
            _fail(
                "network_error",
                "GitHub pull failed; check the configured credential and remote access",
            )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        after = result.refs.get(merge_ref)
        if not after:
            _fail("missing_remote_branch", "configured upstream branch was not found")
        if after == before:
            return {
                **_status_sync(path),
                "before": before.decode(),
                "after": after.decode(),
                "updated": False,
            }
        if (
            not repo.object_store.contains_loose(after)
            and after not in repo.object_store
        ):
            _fail("fetch_failed", "upstream commit was not fetched")
        from dulwich.graph import can_fast_forward

        if not can_fast_forward(repo, before, after):
            _fail(
                "diverged",
                "local and upstream branches have diverged; refusing to merge",
            )
        _validate_tree(repo, after)
        current = _status(repo)
        if (
            current["staged"]
            or current["unstaged"]
            or (current["untracked"] and not allow_untracked)
            or repo.refs[local_ref] != before
        ):
            _fail("changed_during_fetch", "repository changed during fetch")
        protected = {str(value).replace("\\", "/") for value in (protected_paths or ())}
        if protected:
            old_commit, new_commit = repo[before], repo[after]
            changed = {
                os.fsdecode(side.path).replace("\\", "/")
                for change in tree_changes(
                    repo.object_store, old_commit.tree, new_commit.tree
                )
                for side in (change.old, change.new)
                if side is not None
            }
            if protected.intersection(changed):
                _fail(
                    "restore_conflict",
                    "upstream changes overlap saved local changes; nothing was pulled",
                )
        checkout_commit(path, repo, before, after, update_ref=local_ref)
        upstream_suffix = merge_ref[len(b"refs/heads/") :]
        repo.refs[b"refs/remotes/" + remote.encode() + b"/" + upstream_suffix] = after
        return {
            **_status_sync(path),
            "before": before.decode(),
            "after": after.decode(),
            "updated": True,
        }


async def pull_repository(
    repository, token=None, *, allow_untracked=False, protected_paths=None
):
    try:

        def pull(path):
            if allow_untracked or protected_paths:
                return _pull_sync(
                    path,
                    token,
                    allow_untracked=allow_untracked,
                    protected_paths=protected_paths,
                )
            return _pull_sync(path, token)

        return await run_repository_operation(
            repository,
            pull,
        )
    except RepositorySyncError:
        raise
    except Exception:
        raise RepositorySyncError(
            "sync_failed", "repository synchronization failed safely"
        ) from None
