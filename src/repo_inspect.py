"""Read-only git inspection for the Workbench.

Every "what changed?" view — a Claude Code job's edits, the agent worktree's
branch, the bound workspace — used to answer from a different corner of the
code (``_git_report`` in the Claude Code tool, ``diff_summary`` in the worktree
service, nothing at all for the workspace). This module is the one place that
turns a checkout into status, per-file numstat, unified diffs, file contents
at a ref, and commit history, so the UI has one shape to render.

Safety rules, in order:

* **Path confinement.** A repository path is accepted only when it is a git
  checkout inside one of the roots the deployment already trusts for agent
  work: the Claude Code repository roots, the agent worktree root and source
  checkout, and the active workspace. Nothing else on the host is inspectable.
* **argv-only git** through :func:`src.agent_worktree.gitcmd.run_git`, with
  its minimal non-interactive environment. File paths are validated to be
  relative and inside the checkout; refs are validated against a strict
  character class and may never start with ``-`` (no option injection).
* **Bounded output.** Diffs, file bodies and listings are capped so a
  generated file cannot take the UI (or the model that reads the same data)
  down with it.
"""

from __future__ import annotations

import os
import posixpath
import re
from typing import Any, Dict, List, Optional

from src.agent_worktree.gitcmd import GitError, run_git

MAX_DIFF_CHARS = 200_000
MAX_FILE_CHARS = 400_000
MAX_FILES = 500
MAX_COMMITS = 200
_UNTRACKED_LINE_CAP = 20_000

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./~^@{}-]{0,200}$")


class RepoError(ValueError):
    """The request was refused or git could not answer it."""


# ── confinement ──────────────────────────────────────────────────────────────

def allowed_roots() -> List[str]:
    """Directories under which checkouts may be inspected (realpaths)."""
    roots: List[str] = []
    try:
        from src.agent_tools.claude_code_tools import repository_roots

        roots.extend(str(r) for r in repository_roots())
    except Exception:
        pass
    try:
        from src.agent_worktree.config import load_config

        cfg = load_config()
        roots.extend([cfg.worktree_root, cfg.source_repo])
    except Exception:
        pass
    try:
        from src.tool_execution import get_active_workspace

        workspace = get_active_workspace()
        if workspace:
            roots.append(workspace)
    except Exception:
        pass
    seen: set = set()
    out: List[str] = []
    for root in roots:
        try:
            real = os.path.realpath(os.path.expanduser(str(root)))
        except (OSError, ValueError):
            continue
        if real and real not in seen and real != os.sep:
            seen.add(real)
            out.append(real)
    return out


def resolve_repo(path: str, *, roots: Optional[List[str]] = None) -> str:
    """Realpath of a git checkout inside an approved root, or :class:`RepoError`."""
    raw = (path or "").strip()
    if not raw:
        raise RepoError("repository path is required")
    real = os.path.realpath(os.path.expanduser(raw))
    if not os.path.isdir(real):
        raise RepoError(f"{raw!r} is not a directory")
    if not os.path.exists(os.path.join(real, ".git")):
        raise RepoError(f"{raw!r} is not a git checkout or worktree")
    for root in (roots if roots is not None else allowed_roots()):
        if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
            return real
    raise RepoError(
        f"{raw!r} is outside the inspectable roots (Claude Code repository roots, "
        "the agent worktree root, or the active workspace)"
    )


def relative_file(repo: str, file: str) -> str:
    """A validated repository-relative POSIX path, or :class:`RepoError`."""
    raw = (file or "").strip().replace("\\", "/")
    if not raw or "\x00" in raw:
        raise RepoError("file path is required")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise RepoError("file path must be relative to the repository")
    norm = posixpath.normpath(raw)
    if norm in (".", "") or norm.startswith("../") or norm == ".." or norm.startswith("-"):
        raise RepoError("file path escapes the repository")
    real = os.path.realpath(os.path.join(repo, *norm.split("/")))
    if real != repo and not real.startswith(repo.rstrip(os.sep) + os.sep):
        raise RepoError("file path escapes the repository")
    return norm


def validate_ref(value: Optional[str], default: str = "HEAD") -> str:
    ref = (value or "").strip() or default
    if not _REF_RE.match(ref) or ".." in ref.replace("...", ""):
        raise RepoError(f"{value!r} is not a valid git ref")
    return ref


# ── helpers ──────────────────────────────────────────────────────────────────

async def _git(repo: str, *args: str, timeout_s: float = 60.0, ok_codes=(0,)) -> str:
    try:
        res = await run_git(list(args), cwd=repo, timeout_s=timeout_s, check=False)
    except GitError as exc:  # git missing, or the call timed out
        raise RepoError(str(exc))
    if res.code not in ok_codes:
        raise RepoError(f"git {args[0]} failed: {(res.stderr or '').strip()[:300] or 'unknown error'}")
    return res.stdout


def _cap(text: str, limit: int) -> tuple:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n…[truncated]\n", True


def _parse_numstat(text: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        add, dele, path = parts
        binary = add == "-" or dele == "-"
        out[path] = {
            "path": path,
            "additions": 0 if binary else int(add or 0),
            "deletions": 0 if binary else int(dele or 0),
            "binary": binary,
        }
    return out


def _parse_name_status(text: str) -> Dict[str, str]:
    codes: Dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        code, path = parts[0][:1], parts[-1]
        codes[path] = {"A": "added", "D": "deleted", "M": "modified", "R": "renamed",
                       "C": "copied", "T": "type-changed"}.get(code, "modified")
    return codes


async def _untracked(repo: str) -> List[str]:
    out = await _git(repo, "status", "--porcelain", "--untracked-files=all", "--no-renames")
    return [line[3:] for line in out.splitlines() if line.startswith("?? ")]


def _count_lines(repo: str, rel: str) -> int:
    try:
        with open(os.path.join(repo, *rel.split("/")), "rb") as fh:
            data = fh.read(2_000_000)
        if b"\x00" in data[:8000]:
            return 0
        return min(data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1), _UNTRACKED_LINE_CAP)
    except OSError:
        return 0


# ── public API ───────────────────────────────────────────────────────────────

async def head_sha(repo: str) -> Optional[str]:
    try:
        sha = (await _git(repo, "rev-parse", "--verify", "--quiet", "HEAD")).strip()
    except RepoError:
        return None
    return sha or None


async def status(path: str, *, roots: Optional[List[str]] = None) -> Dict[str, Any]:
    repo = resolve_repo(path, roots=roots)
    branch = (await _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", ok_codes=(0, 1, 128))).strip()
    sha = await head_sha(repo)
    porcelain = await _git(repo, "status", "--porcelain", "--untracked-files=all")
    lines = [l for l in porcelain.splitlines() if l]
    upstream = ""
    ahead = behind = 0
    try:
        upstream = (await _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")).strip()
        counts = (await _git(repo, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")).split()
        if len(counts) == 2:
            behind, ahead = int(counts[0]), int(counts[1])
    except RepoError:
        upstream = ""
    return {
        "repository": repo,
        "branch": branch or None,
        "detached": not branch,
        "head": sha,
        "short_head": (sha or "")[:12] or None,
        "upstream": upstream or None,
        "ahead": ahead,
        "behind": behind,
        "dirty": bool(lines),
        "changed_count": len([l for l in lines if not l.startswith("?? ")]),
        "untracked_count": len([l for l in lines if l.startswith("?? ")]),
    }


async def changes(path: str, *, base: Optional[str] = None,
                  roots: Optional[List[str]] = None) -> Dict[str, Any]:
    """Per-file additions/deletions in the working tree relative to ``base``
    (default ``HEAD``): committed-since-base, staged and unstaged edits, plus
    untracked files."""
    repo = resolve_repo(path, roots=roots)
    ref = validate_ref(base, "HEAD")
    files: Dict[str, Dict[str, Any]] = {}
    if await head_sha(repo):
        numstat = _parse_numstat(await _git(repo, "diff", "--numstat", "-M", ref, "--"))
        codes = _parse_name_status(await _git(repo, "diff", "--name-status", "-M", ref, "--"))
        for p, row in numstat.items():
            row["status"] = codes.get(p, "modified")
            files[p] = row
    for rel in await _untracked(repo):
        if rel in files:
            continue
        files[rel] = {"path": rel, "status": "untracked", "additions": _count_lines(repo, rel),
                      "deletions": 0, "binary": False}
    rows = sorted(files.values(), key=lambda r: r["path"])
    truncated = len(rows) > MAX_FILES
    rows = rows[:MAX_FILES]
    return {
        "repository": repo,
        "base": ref,
        "files": rows,
        "truncated": truncated,
        "total_additions": sum(r["additions"] for r in rows),
        "total_deletions": sum(r["deletions"] for r in rows),
    }


async def file_diff(path: str, file: str, *, base: Optional[str] = None,
                    commit: Optional[str] = None, roots: Optional[List[str]] = None) -> Dict[str, Any]:
    """Unified diff of one file: a commit's change, or the working tree against ``base``."""
    repo = resolve_repo(path, roots=roots)
    rel = relative_file(repo, file)
    if commit:
        sha = validate_ref(commit)
        text = await _git(repo, "show", "--format=", "--patch", "-M", sha, "--", rel)
    else:
        ref = validate_ref(base, "HEAD")
        text = ""
        if await head_sha(repo):
            text = await _git(repo, "diff", "-M", ref, "--", rel)
        if not text.strip():
            tracked = (await _git(repo, "ls-files", "--error-unmatch", "--", rel, ok_codes=(0, 1))).strip()
            exists = os.path.isfile(os.path.join(repo, *rel.split("/")))
            if exists and not tracked:
                # Untracked: show the whole file as an addition. --no-index
                # exits 1 whenever the two sides differ, which they always do.
                text = await _git(repo, "diff", "--no-index", "--", os.devnull, rel, ok_codes=(0, 1))
    text, truncated = _cap(text, MAX_DIFF_CHARS)
    return {"repository": repo, "file": rel, "diff": text, "truncated": truncated,
            "binary": "Binary files" in text[:2000]}


async def file_at(path: str, file: str, *, ref: Optional[str] = "HEAD",
                  roots: Optional[List[str]] = None) -> Dict[str, Any]:
    """Contents of one file at ``ref`` (or ``worktree`` for the on-disk copy)."""
    repo = resolve_repo(path, roots=roots)
    rel = relative_file(repo, file)
    if (ref or "").strip().lower() in ("worktree", "wt", "disk"):
        full = os.path.join(repo, *rel.split("/"))
        try:
            with open(full, "rb") as fh:
                data = fh.read(MAX_FILE_CHARS + 1)
        except OSError:
            return {"file": rel, "ref": "worktree", "content": None, "missing": True, "truncated": False}
        text = data.decode("utf-8", errors="replace")
        text, truncated = _cap(text, MAX_FILE_CHARS)
        return {"file": rel, "ref": "worktree", "content": text, "missing": False, "truncated": truncated}
    sha = validate_ref(ref, "HEAD")
    res = await run_git(["show", f"{sha}:{rel}"], cwd=repo, timeout_s=60, check=False)
    if res.code != 0:
        return {"file": rel, "ref": sha, "content": None, "missing": True, "truncated": False}
    text, truncated = _cap(res.stdout, MAX_FILE_CHARS)
    return {"file": rel, "ref": sha, "content": text, "missing": False, "truncated": truncated}


_LOG_FORMAT = "%H%x1f%h%x1f%an%x1f%aI%x1f%s"


async def commits(path: str, *, limit: int = 50, base: Optional[str] = None,
                  roots: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Newest-first commit list; with ``base``, only commits after it."""
    repo = resolve_repo(path, roots=roots)
    if not await head_sha(repo):
        return []
    n = max(1, min(int(limit or 50), MAX_COMMITS))
    args = ["log", f"--format={_LOG_FORMAT}", f"-n{n}"]
    if base:
        args.append(f"{validate_ref(base)}..HEAD")
    out = await _git(repo, *args, "--")
    rows = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 5:
            continue
        sha, short, author, date, subject = parts
        rows.append({"sha": sha, "short": short, "author": author, "date": date, "subject": subject})
    return rows


async def commit_detail(path: str, sha: str, *, roots: Optional[List[str]] = None) -> Dict[str, Any]:
    repo = resolve_repo(path, roots=roots)
    ref = validate_ref(sha)
    header = await _git(repo, "show", "--no-patch", f"--format={_LOG_FORMAT}%x1f%b", ref)
    parts = header.strip("\n").split("\x1f")
    meta = {"sha": parts[0], "short": parts[1], "author": parts[2], "date": parts[3],
            "subject": parts[4], "body": parts[5] if len(parts) > 5 else ""} if len(parts) >= 5 else {"sha": ref}
    numstat = _parse_numstat(await _git(repo, "show", "--format=", "--numstat", "-M", ref, "--"))
    codes = _parse_name_status(await _git(repo, "show", "--format=", "--name-status", "-M", ref, "--"))
    files = []
    for p, row in sorted(numstat.items()):
        row["status"] = codes.get(p, "modified")
        files.append(row)
    patch, truncated = _cap(await _git(repo, "show", "--format=", "--patch", "-M", ref, "--"), MAX_DIFF_CHARS)
    return {"repository": repo, **meta, "files": files[:MAX_FILES], "patch": patch, "truncated": truncated,
            "total_additions": sum(r["additions"] for r in files), "total_deletions": sum(r["deletions"] for r in files)}
