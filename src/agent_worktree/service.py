"""Operations the agent tool and the operator CLI both call.

The shape of the flow, and why each step exists:

1. ``ensure_worktree`` gives the agent one persistent checkout it owns. It is a
   real ``git worktree`` of a repository, on a branch inside that repository's
   agent namespace (``agent/odysseus/`` for the configured source repository,
   ``agent/<repository>/`` for any other approved checkout), living under a
   configured root that every path is re-checked against. The new branch
   starts at an explicit base commit and never tracks anything.
2. The agent edits and tests there with its normal tools, and commits through
   ``commit`` (fixed bot identity, argv-only git).
3. ``request_publish`` freezes the change: it records the exact repo, branch,
   head SHA, file list and sensitive-path classification, and returns them for a
   human to read. It publishes nothing.
4. A human runs the CLI, sees the file list, and grants a short-lived single-use
   approval code.
5. ``publish`` spends that code — re-verifying every bound fact against the live
   worktree, not against what the request claimed — then pushes to THAT
   repository's GitHub remote and opens a **draft** PR.
6. ``cleanup`` detaches a worktree only when nothing would be lost: the tree is
   clean, and the branch is deleted only when another ref still holds its tip.

Every step re-derives state from git rather than trusting a stored value, so a
tampered state file cannot substitute a different change for the approved one.

Repositories. Without ``repository`` every operation works on the configured
source repository (``ODYSSEUS_AGENT_SOURCE_REPO``), exactly as before. With
it, the path must be an approved checkout (repository_sync's roots and
metadata checks); its worktrees live under ``<root>/_repos/<key>/`` and its
publish target is derived from its own ``origin`` remote.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from src.agent_worktree import approval as approval_mod
from src.agent_worktree.config import (
    REPOSITORY_WORKTREE_DIR,
    WorktreeConfig,
    load_config,
    publish_blockers,
)
from src.agent_worktree.gitcmd import GitError, auth_env, git_path, run_git
from src.agent_worktree.locking import LockBusy, file_lock
from src.agent_worktree import sensitive as sensitive_mod
from src.agent_worktree.validation import (
    branch_leaf_from_name,
    is_inside,
    is_valid_repo_slug,
    is_valid_sha,
    normalize_branch,
    safe_worktree_path,
    validate_agent_branch,
)

logger = logging.getLogger(__name__)

MAX_CHANGED_FILES = 500

__all__ = [
    "WorktreeError", "ensure_worktree", "status", "diagnose", "commit", "diff_summary",
    "request_publish", "publish", "cleanup", "remove_worktree", "repository_config",
]


class WorktreeError(RuntimeError):
    """An operation could not be completed safely.

    ``code`` names the failures an agent can fix by itself (see
    agent_tools.worktree_tools, which turns it into a next step).
    """

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        self.code = code


def _lock_path(cfg: WorktreeConfig) -> str:
    return os.path.join(cfg.state_dir, "locks", "worktree.lock")


def files_digest(paths: List[str]) -> str:
    hasher = hashlib.sha256()
    for path in sorted(set(paths)):
        hasher.update(path.encode("utf-8") + b"\n")
    return hasher.hexdigest()


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


# ── git with repository-controlled execution switched off ───────────────────


def _no_hooks_dir(cfg: WorktreeConfig) -> str:
    path = os.path.join(cfg.state_dir, "no-hooks")
    os.makedirs(path, exist_ok=True)
    return path


def _hardened_env(
    cfg: WorktreeConfig, extra: Optional[Dict[str, str]] = None, *, foreign: bool = False
) -> Dict[str, str]:
    """``extra`` plus config that outranks anything in the repository.

    GIT_CONFIG_COUNT entries are command-line scope, so a hook directory or
    fsmonitor command in a repository's (or a worktree's) config cannot run
    inside this process. Appended after ``extra`` so the auth header keeps
    index 0. For repositories other than the configured one, pushes also drop
    every credential helper and every transport except https, so repository
    config cannot hand the publishing credential to a command.
    """
    env = dict(extra or {})
    count = int(env.get("GIT_CONFIG_COUNT") or 0)
    pairs = [("core.hooksPath", _no_hooks_dir(cfg)), ("core.fsmonitor", "false")]
    if foreign:
        pairs += [
            ("credential.helper", ""),
            ("protocol.allow", "never"),
            ("protocol.https.allow", "always"),
            # Signing runs gpg.program; the bot identity does not sign.
            ("commit.gpgSign", "false"),
            ("tag.gpgSign", "false"),
            ("push.gpgSign", "false"),
        ]
    for offset, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{count + offset}"] = key
        env[f"GIT_CONFIG_VALUE_{count + offset}"] = value
    env["GIT_CONFIG_COUNT"] = str(count + len(pairs))
    return env


async def _git(cfg: WorktreeConfig, args: List[str], *, cwd: str,
               extra_env: Optional[Dict[str, str]] = None, **kwargs):
    return await run_git(
        args, cwd=cwd,
        extra_env=_hardened_env(cfg, extra_env, foreign=bool(cfg.repository_key)),
        **kwargs,
    )


async def _head_sha(cfg: WorktreeConfig, worktree: str) -> str:
    res = await _git(cfg, ["rev-parse", "HEAD"], cwd=worktree)
    sha = res.stdout.strip()
    if not is_valid_sha(sha):
        raise WorktreeError("could not read a valid HEAD commit")
    return sha


async def _current_branch(cfg: WorktreeConfig, worktree: str) -> str:
    res = await _git(cfg, ["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=worktree, check=False)
    return res.stdout.strip() if res.ok else ""


async def _dirty_entries(cfg: WorktreeConfig, worktree: str) -> List[str]:
    res = await _git(cfg, ["status", "--porcelain", "--untracked-files=all"], cwd=worktree)
    return [line for line in res.stdout.splitlines() if line.strip()]


async def _is_dirty(cfg: WorktreeConfig, worktree: str) -> bool:
    return bool(await _dirty_entries(cfg, worktree))


# ── which repository ─────────────────────────────────────────────────────────


def _repo_label(slug: str, path: str) -> str:
    raw = (slug.split("/", 1)[1] if slug else os.path.basename(path.rstrip("/\\"))).lower()
    label = re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-._")[:40].rstrip("-._")
    if label.endswith(".lock"):
        label = label[: -len(".lock")]
    if not label or not label[0].isalnum():
        label = "repo" + (("-" + label) if label else "")
    return label


def _repo_key(label: str, path: str) -> str:
    digest = hashlib.sha256(
        os.path.normcase(os.path.realpath(path)).encode("utf-8")
    ).hexdigest()[:8]
    return f"{label}-{digest}"


def _origin_slug(repository: str) -> str:
    """owner/name of the checkout's github.com ``origin``, or "".

    Read from the config file, never by running git in it, and only a
    credential-free https (or scp-style) github.com remote qualifies: the
    publish target is https://github.com/<slug>.git, derived, not configured.
    """
    try:
        from dulwich.repo import Repo

        from src.agent_worktree.repository_sync import RepositorySyncError, _https_url

        with Repo(repository) as repo:
            raw = repo.get_config().get((b"remote", b"origin"), b"url").decode("utf-8")
        url = _https_url(raw)
    except (KeyError, RepositorySyncError, OSError, UnicodeError, ValueError, ImportError):
        return ""
    except Exception:  # noqa: BLE001 - any unreadable config means "no target"
        return ""
    prefix = "https://github.com/"
    if not url.lower().startswith(prefix):
        return ""
    slug = url[len(prefix):]
    if slug.lower().endswith(".git"):
        slug = slug[:-4]
    return slug if is_valid_repo_slug(slug) else ""


async def repository_config(
    cfg: WorktreeConfig, repository: Optional[str]
) -> WorktreeConfig:
    """The configuration for one repository's worktrees.

    ``None``/empty or the configured source repository returns ``cfg``
    unchanged. Anything else must pass repository_sync's approved-root and
    metadata checks; a verified linked worktree is resolved to the repository
    it belongs to.
    """
    raw = str(repository or "").strip()
    if not raw:
        return cfg
    if not os.path.isabs(raw):
        raise WorktreeError(
            f"repository must be an absolute local checkout path, not {raw!r}",
            code="INVALID_REPOSITORY",
        )
    if _same_path(raw, cfg.source_repo):
        return cfg
    try:
        from src.agent_worktree import repository_sync as sync
    except ImportError as exc:  # pragma: no cover - environment problem
        raise WorktreeError(f"repository support is unavailable: {exc}")

    def _validate() -> str:
        path = sync._validate_path(raw, allow_linked=True)
        if (path / ".git").is_file():
            path = sync.linked_worktree_main(path)
        return os.path.realpath(str(path))

    try:
        main = await asyncio.to_thread(_validate)
    except sync.RepositorySyncError as exc:
        raise WorktreeError(f"repository {raw} was refused: {exc}", code="INVALID_REPOSITORY")
    if _same_path(main, cfg.source_repo):
        return cfg
    if is_inside(main, cfg.worktree_root):
        raise WorktreeError(
            f"{main} is inside the agent worktree root; pass the repository it was "
            "created from instead", code="INVALID_REPOSITORY",
        )
    slug = await asyncio.to_thread(_origin_slug, main)
    label = _repo_label(slug, main)
    return dataclasses.replace(
        cfg,
        source_repo=main,
        repo_slug=slug,
        base_branch="",
        branch_prefix=f"agent/{label}/",
        repository_key=_repo_key(label, main),
    )


def _repository_root(cfg: WorktreeConfig) -> str:
    if not cfg.repository_key:
        return cfg.worktree_root
    return os.path.join(cfg.worktree_root, REPOSITORY_WORKTREE_DIR, cfg.repository_key)


def _worktree_dir(cfg: WorktreeConfig, branch: str) -> str:
    path = safe_worktree_path(_repository_root(cfg), branch, cfg.branch_prefix)
    if not path or not is_inside(path, cfg.worktree_root):
        raise WorktreeError(f"branch {branch!r} is not a valid agent branch", code="INVALID_BRANCH")
    return path


# ── per-worktree metadata (state dir: agent file tools cannot write it) ─────


def _meta_dir(cfg: WorktreeConfig) -> str:
    return os.path.join(cfg.state_dir, "worktrees", cfg.repository_key or "_default")


def _meta_path(cfg: WorktreeConfig, branch: str) -> str:
    leaf = branch[len(cfg.branch_prefix):].replace("/", "__")
    return os.path.join(_meta_dir(cfg), f"{leaf}.json")


def _load_meta(cfg: WorktreeConfig, branch: str) -> Dict[str, Any]:
    try:
        with open(_meta_path(cfg, branch), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("branch") == branch else {}


def _save_meta(cfg: WorktreeConfig, branch: str, data: Dict[str, Any]) -> None:
    from core.atomic_io import atomic_write_json

    os.makedirs(_meta_dir(cfg), exist_ok=True)
    atomic_write_json(_meta_path(cfg, branch), data, indent=2)


def _drop_meta(cfg: WorktreeConfig, branch: str) -> None:
    try:
        os.remove(_meta_path(cfg, branch))
    except OSError:
        pass


def _all_meta(cfg: WorktreeConfig) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    base = os.path.join(cfg.state_dir, "worktrees")
    try:
        groups = sorted(os.listdir(base))
    except OSError:
        return out
    for group in groups:
        directory = os.path.join(base, group)
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(directory, name), "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and isinstance(data.get("branch"), str):
                data["_group"] = group
                out.append(data)
    return out


# ── base refs ────────────────────────────────────────────────────────────────


async def _remote_names(cfg: WorktreeConfig) -> List[str]:
    res = await _git(cfg, ["remote"], cwd=cfg.source_repo, check=False)
    return [line.strip() for line in res.stdout.splitlines() if line.strip()] if res.ok else []


async def _looks_like_base(cfg: WorktreeConfig, value: str) -> bool:
    """True when ``value`` names an existing ref or commit rather than a new branch.

    ``{"branch": "origin/main"}`` meant "start from origin/main"; taken as a
    branch name it produced ``agent/odysseus/origin/main`` (2026-09-28).
    """
    text = value.strip().strip("/")
    if not text:
        return False
    first = text.split("/", 1)[0]
    if text == "HEAD" or first in {"refs", "remotes"}:
        return True
    # origin/upstream count even where no such remote is configured: a branch
    # named agent/<repo>/origin/main is wrong in every repository.
    if "/" in text and first in {"origin", "upstream", *await _remote_names(cfg)}:
        return True
    if re.fullmatch(r"[0-9a-fA-F]{7,64}", text):
        res = await _git(cfg, ["rev-parse", "--verify", "--quiet", f"{text}^{{commit}}"],
                         cwd=cfg.source_repo, check=False)
        return res.ok and bool(res.stdout.strip())
    return False


async def _origin_default_branch(cfg: WorktreeConfig) -> str:
    res = await _git(cfg, ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
                     cwd=cfg.source_repo, check=False)
    ref = res.stdout.strip() if res.ok else ""
    prefix = "refs/remotes/origin/"
    return normalize_branch(ref[len(prefix):]) if ref.startswith(prefix) else ""


async def _resolve_base(cfg: WorktreeConfig, base: str) -> Dict[str, str]:
    """{"ref", "sha", "pr_base"} for an explicit base (ref name or commit)."""
    text = (base or "").strip()
    if not normalize_branch(text):
        raise WorktreeError(
            f"base {text!r} is not a usable ref: pass a branch such as origin/main, "
            "a local branch, or a commit SHA", code="INVALID_BASE",
        )
    res = await _git(cfg, ["rev-parse", "--verify", "--quiet", f"{text}^{{commit}}"],
                     cwd=cfg.source_repo, check=False)
    sha = res.stdout.strip() if res.ok else ""
    if not is_valid_sha(sha):
        raise WorktreeError(
            f"base {text!r} does not resolve to a commit in {cfg.source_repo}; fetch it "
            f"first (manage_git fetch with repository={cfg.source_repo}) or check the name",
            code="BASE_NOT_FOUND",
        )
    full = (await _git(cfg, ["rev-parse", "--symbolic-full-name", text],
                       cwd=cfg.source_repo, check=False)).stdout.strip()
    pr_base = ""
    if full.startswith("refs/remotes/"):
        pr_base = full[len("refs/remotes/"):].partition("/")[2]
        if pr_base == "HEAD":
            pr_base = ""
    elif full.startswith("refs/heads/"):
        pr_base = full[len("refs/heads/"):]
    if not pr_base:
        pr_base = await _origin_default_branch(cfg) or cfg.base_branch
    return {"ref": text, "sha": sha, "pr_base": normalize_branch(pr_base)}


async def _default_base(cfg: WorktreeConfig) -> Dict[str, str]:
    """The base used when none is given."""
    if not cfg.repository_key:
        # The configured source repository keeps its configured base branch.
        for candidate in (f"origin/{cfg.base_branch}", cfg.base_branch):
            res = await _git(cfg, ["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"],
                             cwd=cfg.source_repo, check=False)
            if res.ok and is_valid_sha(res.stdout.strip()):
                return {"ref": candidate, "sha": res.stdout.strip(), "pr_base": cfg.base_branch}
        raise WorktreeError(
            f"base branch {cfg.base_branch!r} not found in {cfg.source_repo}; fetch it first",
            code="BASE_NOT_FOUND",
        )
    default = await _origin_default_branch(cfg)
    if not default:
        raise WorktreeError(
            f"{cfg.source_repo} has no origin/HEAD to default to; pass base explicitly "
            "(for example base='origin/main')", code="BASE_REQUIRED",
        )
    return await _resolve_base(cfg, f"origin/{default}")


async def _is_ancestor(cfg: WorktreeConfig, ancestor: str, descendant: str) -> bool:
    res = await _git(cfg, ["merge-base", "--is-ancestor", ancestor, descendant],
                     cwd=cfg.source_repo, check=False)
    return res.ok


def _check_expected(expected_base: Optional[str], resolved: Dict[str, str]) -> None:
    if not expected_base:
        return
    if resolved["sha"] != expected_base:
        raise WorktreeError(
            f"base {resolved['ref']!r} is at {resolved['sha']}, not the expected "
            f"{expected_base}; nothing was created. Fetch, re-check the base, or drop "
            "expected_base if the newer commit is intended.", code="BASE_MISMATCH",
        )


# ── locating a worktree ──────────────────────────────────────────────────────


def _branch_in(cfg: WorktreeConfig, name: str) -> str:
    return validate_agent_branch(name, cfg.branch_prefix) or branch_leaf_from_name(
        name, cfg.branch_prefix
    )


def _worktree_exists(path: str) -> bool:
    return os.path.exists(os.path.join(path, ".git"))


async def _locate(
    cfg: WorktreeConfig, name: str, repository: Optional[str]
) -> Tuple[WorktreeConfig, str, str]:
    """(repository config, branch, path) for a worktree name.

    With ``repository`` the name is resolved in that repository. Without it
    the configured source repository is tried first, then the worktrees
    started in other repositories (recorded in the state directory), so a
    name unique across repositories needs no ``repository`` on later calls.
    """
    if str(repository or "").strip():
        rcfg = await repository_config(cfg, repository)
        branch = _branch_in(rcfg, name)
        if not branch:
            raise WorktreeError("invalid agent branch", code="INVALID_BRANCH")
        return rcfg, branch, _worktree_dir(rcfg, branch)

    branch = _branch_in(cfg, name)
    default_path = _worktree_dir(cfg, branch) if branch else ""
    if default_path and _worktree_exists(default_path):
        return cfg, branch, default_path
    wanted = name.strip().strip("/")
    matches = [
        meta for meta in _all_meta(cfg)
        if meta.get("_group") != "_default"
        and (
            meta.get("branch") == wanted
            or str(meta.get("branch") or "").split("/", 2)[-1] == wanted
        )
    ]
    if len(matches) > 1:
        repos = ", ".join(sorted({str(m.get("repository")) for m in matches}))
        raise WorktreeError(
            f"worktree {wanted!r} exists in several repositories ({repos}); pass repository",
            code="AMBIGUOUS_WORKTREE",
        )
    if matches:
        rcfg = await repository_config(cfg, str(matches[0].get("repository") or ""))
        found = validate_agent_branch(matches[0]["branch"], rcfg.branch_prefix)
        if rcfg.repository_key == matches[0].get("_group") and found:
            return rcfg, found, _worktree_dir(rcfg, found)
    if not branch:
        raise WorktreeError("invalid agent branch", code="INVALID_BRANCH")
    return cfg, branch, default_path


def _verify_membership(cfg: WorktreeConfig, path: str) -> None:
    """The worktree's `.git` pointer must lead back to this repository.

    The agent can write inside its worktree, `.git` pointer file included; a
    pointer to a hand-made gitdir would make every git call here read config
    the agent chose.
    """
    from src.agent_worktree.repository_sync import RepositorySyncError, linked_worktree_main

    try:
        main = str(linked_worktree_main(path))
        source = cfg.source_repo
        if os.path.isfile(os.path.join(source, ".git")):
            # The configured checkout is itself a linked worktree; its
            # worktrees share the main repository's common directory.
            source = str(linked_worktree_main(source))
    except RepositorySyncError as exc:
        raise WorktreeError(f"worktree {path} failed its metadata check: {exc}",
                            code="WORKTREE_METADATA_INVALID")
    if not _same_path(main, source):
        raise WorktreeError(f"worktree {path} does not belong to {cfg.source_repo}",
                            code="WORKTREE_METADATA_INVALID")


def _verify_ok(cfg: WorktreeConfig, path: str) -> bool:
    try:
        _verify_membership(cfg, path)
        return True
    except WorktreeError:
        return False


def _publish_cfg(cfg: WorktreeConfig, branch: str) -> WorktreeConfig:
    """cfg with base_branch set to the branch the PR targets for this worktree."""
    meta = _load_meta(cfg, branch)
    pr_base = normalize_branch(str(meta.get("pr_base") or "")) or cfg.base_branch
    return dataclasses.replace(cfg, base_branch=pr_base) if pr_base != cfg.base_branch else cfg


async def _base_ref(cfg: WorktreeConfig, worktree: str, branch: str) -> str:
    """Ref to diff against: the PR base (remote first), else the recorded base commit."""
    meta = _load_meta(cfg, branch)
    pr_base = normalize_branch(str(meta.get("pr_base") or "")) or cfg.base_branch
    candidates = [f"origin/{pr_base}", pr_base] if pr_base else []
    if is_valid_sha(meta.get("base_sha")):
        candidates.append(meta["base_sha"])
    for candidate in candidates:
        res = await _git(
            cfg, ["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"],
            cwd=worktree, check=False,
        )
        if res.ok and res.stdout.strip():
            return candidate
    raise WorktreeError(
        f"base branch {pr_base or '(none recorded)'!r} not found in the worktree; fetch it first"
    )


async def _changed_files(cfg: WorktreeConfig, worktree: str, branch: str) -> List[str]:
    """Files the branch changes relative to its merge base with the base branch."""
    base = await _base_ref(cfg, worktree, branch)
    res = await _git(cfg, ["diff", "--name-only", f"{base}...HEAD"], cwd=worktree)
    files = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    if len(files) > MAX_CHANGED_FILES:
        raise WorktreeError(
            f"change touches {len(files)} files (limit {MAX_CHANGED_FILES}); "
            "split it into smaller branches"
        )
    return files


# ── start ────────────────────────────────────────────────────────────────────


async def ensure_worktree(
    name: str = "",
    *,
    cfg: Optional[WorktreeConfig] = None,
    repository: Optional[str] = None,
    base: Optional[str] = None,
    expected_base: Optional[str] = None,
    branch: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or reuse the persistent worktree for one agent branch.

    ``name`` is the task name; ``branch`` optionally names the new branch
    (inside the repository's agent namespace). ``base`` is where a NEW branch
    starts — a ref such as origin/main, a local branch, or a commit — and
    ``expected_base`` (a full SHA) makes the call refuse unless ``base``
    resolves to exactly that commit. A ``branch`` that names an existing ref
    (origin/main, refs/..., a commit) is taken as the base, never as a branch.

    Editing and testing do not require publishing to be enabled — the isolation
    is useful on its own — so this deliberately does not check the publish flag.
    """
    cfg = cfg or load_config()
    if not git_path():
        raise WorktreeError("git is not installed or not on PATH")
    rcfg = await repository_config(cfg, repository)
    if not os.path.exists(os.path.join(rcfg.source_repo, ".git")):
        raise WorktreeError(f"source repository {rcfg.source_repo} is not a git checkout")
    notes: List[str] = []
    base = str(base or "").strip() or None
    expected = str(expected_base or "").strip().lower() or None
    if expected and not is_valid_sha(expected):
        raise WorktreeError("expected_base must be a full 40- or 64-character commit SHA",
                            code="INVALID_BASE")

    requested = str(branch or "").strip()
    name = str(name or "").strip()
    for field, value in (("branch", requested), ("name", name)):
        if value and await _looks_like_base(rcfg, value):
            if base and base != value:
                raise WorktreeError(
                    f"{field} {value!r} names an existing ref or commit, not a new branch, "
                    f"and base {base!r} was also given; pass the task name as 'name' and "
                    "the starting point as 'base'", code="BRANCH_IS_BASE",
                )
            base = value
            notes.append(
                f"{field} {value!r} names an existing ref, so it was used as the base; "
                "branches are never named after a base ref."
            )
            if field == "branch":
                requested = ""
            else:
                name = ""
    target = requested or name
    if not target:
        raise WorktreeError(
            "a task name is required ('name', e.g. 'checkin-fix'); pass the starting "
            "point separately as 'base'", code="MISSING_BRANCH",
        )
    new_branch = _branch_in(rcfg, target)
    if not new_branch:
        raise WorktreeError(
            "worktree name must form a valid branch under " + rcfg.branch_prefix,
            code="INVALID_BRANCH",
        )
    leaf = new_branch[len(rcfg.branch_prefix):]
    if await _looks_like_base(rcfg, leaf):
        raise WorktreeError(
            f"branch {new_branch!r} would be named after the ref {leaf!r}; use a task name "
            f"and pass base={leaf!r}", code="BRANCH_IS_BASE",
        )

    path = _worktree_dir(rcfg, new_branch)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    resolved_base: Optional[Dict[str, str]] = None

    try:
        with file_lock(_lock_path(cfg)):
            # Reuse an existing, healthy worktree.
            if os.path.lexists(path):
                _verify_membership(rcfg, path)
                current = await _current_branch(rcfg, path)
                if current != new_branch:
                    raise WorktreeError(
                        f"worktree {path} is on {current or 'a detached HEAD'}, "
                        f"expected {new_branch}"
                    )
                if base or expected:
                    # Reusing is only right when the worktree started where the
                    # caller wants to start. A worktree recorded before bases
                    # were tracked passes when it contains the wanted commit.
                    recorded = _load_meta(rcfg, new_branch).get("base_sha")
                    if base:
                        resolved_base = await _resolve_base(rcfg, base)
                        _check_expected(expected, resolved_base)
                        wanted_sha, wanted = resolved_base["sha"], f"{resolved_base['ref']} ({resolved_base['sha']})"
                    else:
                        wanted_sha, wanted = expected, str(expected)
                    if recorded != wanted_sha and not (
                        not recorded and await _is_ancestor(rcfg, wanted_sha, new_branch)
                    ):
                        raise WorktreeError(
                            f"worktree {new_branch} already exists and started from "
                            f"{recorded or 'another base'}, not {wanted}; pick a new name "
                            "for work on that base", code="BASE_MISMATCH",
                        )
                out = await status(new_branch, cfg=cfg, repository=rcfg.source_repo if rcfg.repository_key else None)
                if notes:
                    out["notes"] = notes
                return out

            branch_exists = (
                await _git(
                    rcfg, ["rev-parse", "--verify", "--quiet", f"refs/heads/{new_branch}"],
                    cwd=rcfg.source_repo, check=False,
                )
            ).stdout.strip()

            if base:
                resolved_base = await _resolve_base(rcfg, base)
            elif not branch_exists:
                resolved_base = await _default_base(rcfg)
            if resolved_base is not None:
                _check_expected(expected, resolved_base)
            elif expected:
                # Existing branch, no base named: the expectation is about where
                # the branch starts, so it must contain the expected commit.
                if not await _is_ancestor(rcfg, expected, new_branch):
                    raise WorktreeError(
                        f"branch {new_branch} does not contain the expected base {expected}",
                        code="BASE_MISMATCH",
                    )

            # A stale registration of THIS path (directory deleted) blocks `add`.
            # Pruning only removes registrations whose directory is gone.
            listing = await _git(rcfg, ["worktree", "list", "--porcelain"],
                                 cwd=rcfg.source_repo, check=False)
            if any(
                line.startswith("worktree ") and _same_path(line[len("worktree "):], path)
                for line in listing.stdout.splitlines()
            ):
                await _git(rcfg, ["worktree", "prune"], cwd=rcfg.source_repo, check=False)

            if branch_exists:
                if resolved_base is not None and not await _is_ancestor(
                    rcfg, resolved_base["sha"], new_branch
                ):
                    raise WorktreeError(
                        f"branch {new_branch} already exists and is not based on "
                        f"{resolved_base['ref']} ({resolved_base['sha']}); pick a new name",
                        code="BASE_MISMATCH",
                    )
                args = ["worktree", "add", path, new_branch]
            else:
                # The start point is the resolved commit, not the ref name: the
                # ref could move between the check and the add, and a remote ref
                # as start point would make the branch track (and later pull
                # from or push to) that remote branch.
                args = ["worktree", "add", "-b", new_branch, path, resolved_base["sha"]]
            await _git(rcfg, args, cwd=rcfg.source_repo, timeout_s=300)
            previous = _load_meta(rcfg, new_branch)
            _save_meta(rcfg, new_branch, {
                "repository": rcfg.source_repo,
                "repo_slug": rcfg.repo_slug or None,
                "branch": new_branch,
                "path": path,
                "base": (resolved_base or {}).get("ref") or previous.get("base"),
                "base_sha": (resolved_base or {}).get("sha") or previous.get("base_sha"),
                "pr_base": (resolved_base or {}).get("pr_base") or previous.get("pr_base")
                or rcfg.base_branch or None,
                "created_at": previous.get("created_at") or time.time(),
            })
    except LockBusy as exc:
        raise WorktreeError(str(exc))
    except GitError as exc:
        raise WorktreeError(str(exc))

    logger.info(
        "agent worktree: ready repo=%s branch=%s base=%s@%s path=%s",
        rcfg.source_repo, new_branch, (resolved_base or {}).get("ref"),
        ((resolved_base or {}).get("sha") or "")[:12], path,
    )
    out = await status(new_branch, cfg=cfg, repository=rcfg.source_repo if rcfg.repository_key else None)
    if notes:
        out["notes"] = notes
    return out


# ── status ───────────────────────────────────────────────────────────────────


def _worktree_dirs(cfg: WorktreeConfig) -> List[str]:
    """Every worktree directory: the source repository's at the root, other
    repositories' under _repos/<key>/."""
    out: List[str] = []
    try:
        names = sorted(os.listdir(cfg.worktree_root))
    except OSError:
        names = []
    for entry in names:
        path = os.path.join(cfg.worktree_root, entry)
        if entry == REPOSITORY_WORKTREE_DIR:
            try:
                groups = sorted(os.listdir(path))
            except OSError:
                continue
            for group in groups:
                group_path = os.path.join(path, group)
                if not os.path.isdir(group_path) or os.path.islink(group_path):
                    continue
                try:
                    leaves = sorted(os.listdir(group_path))
                except OSError:
                    continue
                out.extend(os.path.join(group_path, leaf) for leaf in leaves)
            continue
        out.append(path)
    return [p for p in out if os.path.isdir(p) and is_inside(p, cfg.worktree_root)]


def _main_repository_of(path: str) -> Optional[str]:
    try:
        from src.agent_worktree.repository_sync import linked_worktree_main

        return str(linked_worktree_main(path))
    except Exception:  # noqa: BLE001 - reported as unknown
        return None


async def status(
    branch: Optional[str] = None,
    *,
    cfg: Optional[WorktreeConfig] = None,
    repository: Optional[str] = None,
    owner: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Configuration and worktree state. Contains no credential material.

    ``owner`` and ``session_id`` scope the per-branch ``publish`` block to the
    requests this person's chat (and its workers) made.
    """
    cfg = cfg or load_config()

    def _publish_block(name: Optional[str], head: Optional[str], base_sha: Optional[str]) -> Optional[Dict[str, Any]]:
        # Read from the request records, never recalled from earlier turns:
        # 2026-10-02 a worker reported a spent request as "waiting for approval".
        if not name or not head:
            return None
        try:
            return approval_mod.publish_status_for_branch(
                name, head_sha=head, base_sha=base_sha, repo=rcfg.repo_slug or None,
                owner=owner, session_id=session_id, cfg=cfg,
            )
        except Exception as exc:  # noqa: BLE001 - status must still answer
            logger.debug("agent worktree: publish status unavailable: %s", exc)
            return None

    rcfg = await repository_config(cfg, repository)
    out: Dict[str, Any] = {
        "publish_enabled": cfg.publish_enabled,
        "repo": rcfg.repo_slug or None,
        # Which checkout this tool operates on. Without it the agent could not
        # tell that a worktree it just started belongs to Odysseus rather than
        # to the third-party project it was actually working in.
        "source_repo": cfg.source_repo,
        "repository": rcfg.source_repo,
        "base_branch": rcfg.base_branch or None,
        "branch_prefix": rcfg.branch_prefix,
        "worktree_root": cfg.worktree_root,
        "approval_ttl_seconds": cfg.approval_ttl_s,
        "credential": (
            "github_app" if cfg.has_github_app
            else ("fallback_token" if cfg.has_any_credential else None)
        ),
        "publish_blockers": publish_blockers(rcfg),
        "worktrees": [],
        "hint": (
            "Without 'repository' this tool works on " + cfg.source_repo + ". For any other "
            "project pass repository=<absolute checkout path> (and base=<ref> on start)."
        ),
    }
    # A named call is one worktree, not an inventory of every project.
    if branch:
        lcfg, resolved, path = await _locate(cfg, branch, repository)
        rcfg = lcfg
        meta = _load_meta(lcfg, resolved)
        out.update(repository=lcfg.source_repo, repo=lcfg.repo_slug or None,
                   branch_prefix=lcfg.branch_prefix, base_branch=lcfg.base_branch or None,
                   publish_blockers=publish_blockers(lcfg), branch=resolved, path=path,
                   exists=os.path.lexists(path), base=meta.get("base"),
                   base_sha=meta.get("base_sha"), pr_base=meta.get("pr_base") or lcfg.base_branch or None)
        if out["exists"]:
            try:
                _verify_membership(lcfg, path)
            except WorktreeError as exc:
                out.update(error=str(exc), code=exc.code,
                           next_action={"action": "diagnose", "repository": lcfg.source_repo,
                                        "branch": resolved})
            else:
                current = await _current_branch(lcfg, path)
                out.update(head_sha=await _head_sha(lcfg, path), dirty=await _is_dirty(lcfg, path),
                           current_branch=current)
                if current != resolved:
                    out.update(code="WORKTREE_BRANCH_MISMATCH",
                               error=f"worktree is on {current or 'a detached HEAD'}, expected {resolved}")
                block = _publish_block(resolved, out["head_sha"], meta.get("base_sha"))
                if block:
                    out["publish"] = block
            out["worktrees"] = [{key: value for key, value in out.items() if key in {
                "path", "repository", "branch", "current_branch", "head_sha", "dirty", "base",
                "base_sha", "publish", "error", "code", "next_action"}}]
        return out
    metas = {
        os.path.normcase(os.path.realpath(str(m.get("path") or ""))): m
        for m in _all_meta(cfg) if m.get("path")
    }
    omitted = 0
    for path in _worktree_dirs(cfg):
        main = _main_repository_of(path)
        if (repository or rcfg.repository_key) and (main is None or not _same_path(main, rcfg.source_repo)):
            continue
        if len(out["worktrees"]) >= 50:
            omitted += 1
            continue
        meta = metas.get(os.path.normcase(os.path.realpath(path)), {})
        # git runs in a worktree only when its pointer leads to a repository
        # this tool created it from: the agent can rewrite a worktree's `.git`
        # file, and git would read whatever config the new target holds.
        known = main is not None and (
            _verify_ok(cfg, path)
            or (meta.get("repository") and _same_path(main, str(meta["repository"])))
        )
        if not known:
            out["worktrees"].append({"path": path, "repository": main, "branch": None,
                                     "head_sha": None, "dirty": None,
                                     "error": "worktree metadata could not be verified"})
            continue
        gcfg = cfg if _verify_ok(cfg, path) else dataclasses.replace(
            cfg, repository_key=str(meta.get("_group") or "other"))
        try:
            entry_branch = await _current_branch(gcfg, path)
            info = {
                "path": path,
                "repository": main,
                "branch": entry_branch,
                "head_sha": await _head_sha(gcfg, path) if entry_branch else None,
                "dirty": await _is_dirty(gcfg, path),
            }
        except (GitError, WorktreeError):
            info = {"path": path, "repository": main, "branch": None, "head_sha": None, "dirty": None}
        if meta.get("base"):
            info["base"] = meta.get("base")
            info["base_sha"] = meta.get("base_sha")
        block = _publish_block(info.get("branch"), info.get("head_sha"), meta.get("base_sha"))
        if block:
            info["publish"] = block
        out["worktrees"].append(info)

    if omitted:
        out.update(worktrees_omitted=omitted,
                   hint="Inventory limited to 50 entries. Pass repository and branch for one worktree.")
    return out


def _expected_head(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not is_valid_sha(text):
        raise WorktreeError("expected_head must be a full 40- or 64-character commit SHA",
                            code="INVALID_EXPECTED_HEAD")
    return text


async def _diagnostic_git(cfg: WorktreeConfig, args: List[str], *, cwd: str,
                          check: bool = True):
    # Missing objects in a promisor repository otherwise trigger an implicit
    # fetch. The protocol allowlist is a fail-closed fallback for older Git;
    # optional locks off also prevents status from refreshing the index.
    return await _git(cfg, args, cwd=cwd, check=check, extra_env={
        "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "", "GIT_OPTIONAL_LOCKS": "0",
    })


async def diagnose(
    branch: str, *, cfg: Optional[WorktreeConfig] = None,
    repository: Optional[str] = None, expected_head: Optional[str] = None,
    workspace: Optional[str] = None,
) -> Dict[str, Any]:
    """Read-only local publication identity check. No credentials or network.

    Git runs only against verified managed metadata. The active workspace's
    object-store path is read from the filesystem, never its Git config.
    """
    from pathlib import Path
    from src.agent_worktree.ownership import git_common_dir

    cfg = cfg or load_config()
    expected = _expected_head(expected_head)
    rcfg, resolved, path = await _locate(cfg, branch, repository)
    where = {"repository": rcfg.source_repo, "branch": resolved}
    out: Dict[str, Any] = {**where, "path": path, "expected_head": expected,
                           "read_only": True, "network_used": False}
    if not os.path.lexists(path):
        return {**out, "code": "WORKTREE_NOT_STARTED",
                "next_action": {"action": "start", **where},
                "hint": "Create the registered worktree before editing. Do not make a replacement clone."}
    try:
        _verify_membership(rcfg, path)
    except WorktreeError as exc:
        return {**out, "code": "WORKTREE_METADATA_INVALID", "error": str(exc),
                "recovery": {"requires": "host_metadata_repair", "repository": rcfg.source_repo,
                             "path": path},
                "hint": "The host cannot verify this pointer. Preserve files and refs; do not rewrite .git, "
                        "reset, or clone over it. An administrator must repair the registered metadata."}
    head = await _diagnostic_git(rcfg, ["rev-parse", "HEAD"], cwd=path)
    current = await _diagnostic_git(rcfg, ["symbolic-ref", "--quiet", "--short", "HEAD"],
                                    cwd=path, check=False)
    pending = await _diagnostic_git(rcfg, ["status", "--porcelain", "--untracked-files=normal"], cwd=path)
    out.update(head_sha=head.stdout.strip(), current_branch=current.stdout.strip() if current.ok else "",
               dirty=bool(pending.stdout.strip()))
    common = git_common_dir(Path(path))
    out["git_common_dir"] = str(common) if common else None
    if workspace:
        workspace_common = git_common_dir(Path(workspace))
        out["workspace_shares_objects"] = bool(common and workspace_common == common)
    if expected:
        available = await _diagnostic_git(rcfg, ["cat-file", "-e", f"{expected}^{{commit}}"],
                                          cwd=path, check=False)
        out["expected_head_available"] = available.ok
    if out["current_branch"] != resolved:
        code = "WORKTREE_BRANCH_MISMATCH"
        hint = "Preserve both refs. Review the registered worktree's branch before committing or publishing."
    elif expected and expected != out["head_sha"]:
        code = "HEAD_MISMATCH" if out["expected_head_available"] else "OBJECT_STORE_DRIFT"
        hint = ("The tested HEAD is not the registered HEAD. A separate clone does not update this branch. "
                "Preserve both histories. Integrate the tested commit into the registered worktree through "
                "an approved local Git route, then verify the resulting tree and rerun affected checks. "
                "Do not change publishing credentials or make another request to bypass this.")
    elif out.get("workspace_shares_objects") is False:
        code = "WORKSPACE_OBJECT_STORE_MISMATCH"
        hint = "This workspace uses a different object store. Work in the registered path above, not a clone."
    else:
        code = "WORKTREE_DIRTY" if out["dirty"] else "READY"
        hint = ("Inspect and commit the owned changes in the registered worktree." if out["dirty"]
                else "Local identity verified. Pass the tested HEAD as expected_head for publication. "
                     "Human approval and credential checks still apply.")
    out.update(code=code, hint=hint, next_action={"action": "diff", **where})
    return out


# ── commit / diff ────────────────────────────────────────────────────────────


async def _started(
    cfg: WorktreeConfig, branch: str, repository: Optional[str]
) -> Tuple[WorktreeConfig, str, str]:
    rcfg, resolved, path = await _locate(cfg, branch, repository)
    if not _worktree_exists(path):
        if os.path.lexists(path):
            _verify_membership(rcfg, path)
        raise WorktreeError("worktree does not exist yet; start it first", code="WORKTREE_NOT_STARTED")
    _verify_membership(rcfg, path)
    return rcfg, resolved, path


async def commit(
    branch: str,
    message: str,
    *,
    cfg: Optional[WorktreeConfig] = None,
    repository: Optional[str] = None,
) -> Dict[str, Any]:
    """Stage everything in the worktree and commit it under the bot identity."""
    cfg = cfg or load_config()
    text = (message or "").strip()
    rcfg, resolved, path = await _locate(cfg, branch, repository)
    if not text:
        raise WorktreeError("a commit message is required")
    if not _worktree_exists(path):
        raise WorktreeError("worktree does not exist yet; start it first", code="WORKTREE_NOT_STARTED")
    _verify_membership(rcfg, path)

    try:
        with file_lock(_lock_path(cfg)):
            current = await _current_branch(rcfg, path)
            if current != resolved:
                raise WorktreeError(
                    f"worktree is on {current or 'a detached HEAD'}, expected {resolved}"
                )
            if not await _is_dirty(rcfg, path):
                return {"committed": False, "reason": "nothing to commit",
                        "head_sha": await _head_sha(rcfg, path)}
            await _git(rcfg, ["add", "-A", "--", "."], cwd=path)
            # -- separates the message from anything that could look like a flag.
            await _git(rcfg, ["commit", "-m", text[:4000], "--no-verify"], cwd=path,
                       timeout_s=300)
            return {"committed": True, "head_sha": await _head_sha(rcfg, path),
                    "repository": rcfg.source_repo, "branch": resolved}
    except LockBusy as exc:
        raise WorktreeError(str(exc))
    except GitError as exc:
        raise WorktreeError(str(exc))


async def _summary(rcfg: WorktreeConfig, resolved: str, path: str) -> Dict[str, Any]:
    try:
        # The summary reports HEAD, and publish pushes the branch. If those two
        # have been separated — a detached HEAD parked on the reviewed commit
        # while the branch ref points at something else — the operator would
        # approve one commit and a different one would be published.
        current = await _current_branch(rcfg, path)
        if current != resolved:
            raise WorktreeError(
                f"worktree is on {current or 'a detached HEAD'}, expected {resolved}"
            )
        files = await _changed_files(rcfg, path, resolved)
        findings = sensitive_mod.classify(files)
        return {
            "repository": rcfg.source_repo,
            "branch": resolved,
            "base": await _base_ref(rcfg, path, resolved),
            "head_sha": await _head_sha(rcfg, path),
            "dirty": await _is_dirty(rcfg, path),
            "changed_files": files,
            "files_digest": files_digest(files),
            "sensitive": findings,
            "sensitive_digest": sensitive_mod.digest(findings),
            "sensitive_summary": sensitive_mod.summarize(findings),
        }
    except GitError as exc:
        raise WorktreeError(str(exc))


async def diff_summary(
    branch: str,
    *,
    cfg: Optional[WorktreeConfig] = None,
    repository: Optional[str] = None,
) -> Dict[str, Any]:
    """Changed files plus the sensitive-path classification for them.

    ``changed_files`` is what the branch has COMMITTED since its base, because
    that is what a publish would push and what the approval digests cover. A
    worker that edits, tests and then diffs before committing got
    ``changed_files: []`` next to ``dirty: true`` and could read that as "no
    changes" (the 2026-09-28 orchestration e2e run: two edited files, an empty
    list). The uncommitted paths are listed separately, outside the digests.
    """
    cfg = cfg or load_config()
    rcfg, resolved, path = await _started(cfg, branch, repository)
    summary = await _summary(rcfg, resolved, path)
    if summary.get("dirty"):
        try:
            pending = _porcelain_paths(await _dirty_entries(rcfg, path))
        except (GitError, WorktreeError):
            pending = []
        summary["uncommitted_files"] = pending
        summary["note"] = (
            "changed_files lists committed changes only; uncommitted_files are edits in the "
            "worktree that are not committed yet. Commit them (action='commit') to include them."
        )
    return summary


def _porcelain_paths(entries: List[str]) -> List[str]:
    """Paths from ``git status --porcelain`` lines ("XY path", "XY old -> new")."""
    paths: List[str] = []
    for line in entries:
        rest = line[3:] if len(line) > 3 else ""
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        rest = rest.strip()
        if len(rest) >= 2 and rest[0] == rest[-1] == '"':
            rest = rest[1:-1]
        if rest and rest not in paths:
            paths.append(rest)
    return paths[:MAX_CHANGED_FILES]


# ── publishing ───────────────────────────────────────────────────────────────

# Config a repository other than the configured one may not carry when the
# publishing credential is about to be used from inside it: each can rewrite
# where the push goes or make git run a command that inherits the credential.
# (Hooks, fsmonitor, credential helpers and non-https transports are also
# switched off by _hardened_env.)
_FOREIGN_REFUSED_SECTIONS = frozenset({"url", "http", "include", "includeif", "protocol", "filter"})
_FOREIGN_REFUSED_CORE_KEYS = frozenset({"sshcommand", "askpass", "gitproxy", "hookspath", "worktree"})


def _foreign_publish_blockers(cfg: WorktreeConfig) -> List[str]:
    if not cfg.repository_key:
        return []
    try:
        from dulwich.config import ConfigFile

        config = ConfigFile.from_path(os.path.join(cfg.source_repo, ".git", "config"))
    except Exception:  # noqa: BLE001
        return [f"{cfg.source_repo}/.git/config could not be read safely"]
    refused = set()
    for section in config.sections():
        head = section[0].decode("utf-8", "replace").lower()
        if head in _FOREIGN_REFUSED_SECTIONS:
            refused.add(f"[{head}]")
        elif head == "core":
            for key, _value in config.items(section):
                if key.decode("utf-8", "replace").lower() in _FOREIGN_REFUSED_CORE_KEYS:
                    refused.add(f"core.{key.decode('utf-8', 'replace')}")
    if not refused:
        return []
    return [
        f"{cfg.source_repo}/.git/config sets {', '.join(sorted(refused))}, which could redirect "
        "the push or run a command holding the publishing credential; the operator must remove it"
    ]


async def _remote_head(cfg: WorktreeConfig, branch: str, token: Optional[str]) -> Optional[str]:
    """Current SHA of the branch on the remote, or None when it is absent.

    Raises on a lookup failure so the caller can record "unknown" explicitly
    rather than mistaking a network error for an absent branch.
    """
    res = await _git(
        cfg,
        ["ls-remote", "--heads", cfg.remote_url, f"refs/heads/{branch}"],
        cwd=cfg.source_repo,
        extra_env=auth_env(token, cfg.remote_url),
        secrets=(token,) if token else (),
        timeout_s=60,
    )
    line = res.stdout.strip().splitlines()
    if not line:
        return None
    sha = line[0].split("\t", 1)[0].strip()
    return sha if is_valid_sha(sha) else None


async def request_publish(
    branch: str,
    *,
    title: str,
    body: str = "",
    requested_by: Optional[str] = None,
    cfg: Optional[WorktreeConfig] = None,
    repository: Optional[str] = None,
    session_id: Optional[str] = None,
    expected_head: Optional[str] = None,
) -> Dict[str, Any]:
    """Freeze the change and record an approval request. Publishes nothing."""
    cfg = cfg or load_config()
    expected = _expected_head(expected_head)
    rcfg, resolved, path = await _started(cfg, branch, repository)
    # Reject a clone's tested receipt before credentials, network or request state.
    if expected:
        actual = await _head_sha(rcfg, path)
        if actual != expected:
            raise WorktreeError(f"registered worktree HEAD is {actual}, not tested HEAD {expected}; "
                                "call diagnose with the same repository, branch and expected_head. "
                                "Nothing was requested or published.", code="HEAD_MISMATCH")
    pcfg = _publish_cfg(rcfg, resolved)
    blockers = publish_blockers(pcfg) + _foreign_publish_blockers(pcfg)
    if not pcfg.base_branch:
        blockers.append("no base branch is recorded for this worktree to open the PR against; "
                        "start a new worktree with base=origin/<branch>")
    if blockers:
        raise WorktreeError("publishing is not available: " + "; ".join(blockers))

    summary = await _summary(pcfg, resolved, path)
    if expected and summary["head_sha"] != expected:
        raise WorktreeError("HEAD moved during publication preflight; diagnose and review again",
                            code="HEAD_MISMATCH")
    if summary["dirty"]:
        raise WorktreeError(
            "worktree has uncommitted changes; commit them so the approval "
            "covers exactly what would be pushed"
        )
    if not summary["changed_files"]:
        raise WorktreeError("branch has no changes relative to the base branch")

    # A change to a lockfile or build manifest means whoever runs the app must
    # reinstall after pulling; the PR says so, and the agent is told to.
    from src.agent_worktree.dependencies import dependency_changes, reinstall_note

    deps = dependency_changes(summary["changed_files"])
    note = reinstall_note(deps)
    if note and "Dependencies changed" not in (body or ""):
        body = f"{(body or '').rstrip()}\n\n{note}".strip()

    from src.agent_worktree.github import GitHubError, resolve_token

    try:
        token = await resolve_token(pcfg)
        remote_head = await _remote_head(pcfg, summary["branch"], token)
        remote_state = remote_head or "absent"
    except (GitHubError, GitError) as exc:
        # Recorded as unknown: publish then refuses a force-push and does a
        # plain push, which fails loudly if the remote branch moved.
        logger.warning("agent worktree: remote head lookup failed: %s", exc)
        remote_state = "unknown"
    finally:
        token = None

    record = approval_mod.create_request(
        repo=pcfg.repo_slug,
        repository=pcfg.source_repo if pcfg.repository_key else None,
        branch=summary["branch"],
        head_sha=summary["head_sha"],
        base_branch=pcfg.base_branch,
        title=(title or summary["branch"])[:250],
        body=(body or "")[:60000],
        changed_files=summary["changed_files"],
        files_digest=summary["files_digest"],
        sensitive=summary["sensitive"],
        sensitive_digest=summary["sensitive_digest"],
        remote_state=remote_state,
        requested_by=requested_by,
        session_id=session_id,
        cfg=cfg,
    )
    view = approval_mod.public_view(record)
    view["remote_state"] = remote_state
    view["sensitive_summary"] = summary["sensitive_summary"]
    if deps:
        view["dependency_changes"] = deps
        view["dependency_note"] = (
            "This change alters dependencies: "
            + "; ".join(f"{d['kind']} in {d['folder']}" for d in deps)
            + ". The PR body says to reinstall after pulling; tell the user too ("
            + "; ".join(f"`{d['command']}` in {d['folder']}" for d in deps) + ")."
        )
    view["next_step"] = (
        "A person must approve it: in the Odysseus UI (this chat shows the publish request "
        "with Review; approving there also publishes), or on the host with "
        f"scripts/odysseus-agent-worktree approve {record['id']}"
        + (" --allow-sensitive" if summary["sensitive"] else "")
        + ". You cannot approve it yourself."
    )
    return view


async def publish(
    request_id: str,
    approval_code: str,
    *,
    cfg: Optional[WorktreeConfig] = None,
) -> Dict[str, Any]:
    """Spend an approval, push the branch, and open a draft PR."""
    cfg = cfg or load_config()
    stored = approval_mod.get_request(request_id, cfg=cfg)
    # The repository comes from the request record (state dir, which the
    # agent's file tools cannot write) and is re-validated like any other
    # argument; the push target is still bound by the approval to `repo`.
    rcfg = await repository_config(cfg, stored.get("repository"))
    branch = validate_agent_branch(stored.get("branch") or "", rcfg.branch_prefix)
    if not branch:
        raise WorktreeError("stored request does not name a valid agent branch")
    pcfg = _publish_cfg(rcfg, branch)
    blockers = publish_blockers(pcfg) + _foreign_publish_blockers(pcfg)
    if blockers:
        raise WorktreeError("publishing is not available: " + "; ".join(blockers))
    if stored.get("repo") != pcfg.repo_slug:
        raise WorktreeError(
            "request targets a different repository than the configured one"
        )

    # Held across verification and push so a concurrent commit cannot move the
    # branch between the change being checked and the change being sent.
    try:
        lock = file_lock(_lock_path(cfg))
        lock.__enter__()
    except LockBusy as exc:
        raise WorktreeError(str(exc))
    try:
        return await _publish_locked(pcfg, request_id, approval_code, stored, branch)
    finally:
        lock.__exit__(None, None, None)


async def _publish_locked(
    cfg: WorktreeConfig,
    request_id: str,
    approval_code: str,
    stored: Dict[str, Any],
    branch: str,
) -> Dict[str, Any]:
    # Re-derive the change from git. The approval is checked against what is on
    # disk right now, never against the numbers the request recorded earlier.
    path = _worktree_dir(cfg, branch)
    if not _worktree_exists(path):
        raise WorktreeError("worktree does not exist yet; start it first", code="WORKTREE_NOT_STARTED")
    _verify_membership(cfg, path)
    live = await _summary(cfg, branch, path)
    if live["dirty"]:
        raise WorktreeError("worktree has uncommitted changes; commit or discard them")

    approval_mod.consume(
        request_id,
        approval_code,
        repo=cfg.repo_slug,
        branch=branch,
        head_sha=live["head_sha"],
        files_digest=live["files_digest"],
        sensitive_digest=live["sensitive_digest"],
        has_sensitive=bool(live["sensitive"]),
        cfg=cfg,
    )
    approved_sha = live["head_sha"]
    try:
        return await _push_approved(cfg, request_id, stored, branch, live, approved_sha)
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised unchanged
        # The grant is already spent (consume() runs first on purpose), so a
        # failure here leaves a used request with nothing published. Say so on
        # the record; otherwise it reads as published and a second approval
        # answers "already published" (2026-10-02).
        try:
            approval_mod.mark_failed(request_id, str(exc), cfg=cfg)
        except Exception:  # noqa: BLE001 - never mask the push failure
            logger.warning("agent worktree: could not mark request %s failed", request_id, exc_info=True)
        raise


async def _push_approved(
    cfg: WorktreeConfig,
    request_id: str,
    stored: Dict[str, Any],
    branch: str,
    live: Dict[str, Any],
    approved_sha: str,
) -> Dict[str, Any]:
    """Push the approved commit and open the draft PR; the grant is spent."""
    from src.agent_worktree.github import (
        GitHubError,
        create_draft_pr,
        find_open_pr,
        forget_token,
        resolve_token,
    )

    token = None
    try:
        token = await resolve_token(cfg)
        expected_remote = str(stored.get("remote_state") or "unknown")
        observed = await _remote_head(cfg, branch, token)
        observed_state = observed or "absent"
        if expected_remote != "unknown" and observed_state != expected_remote:
            raise WorktreeError(
                "the remote branch moved since the approval was requested; "
                "request a new approval"
            )

        push_args = ["push", "--no-verify"]
        if expected_remote not in ("unknown", "absent"):
            # Bound to the exact remote SHA the operator approved against, so a
            # concurrent push cannot be silently overwritten.
            push_args.append(f"--force-with-lease=refs/heads/{branch}:{expected_remote}")
        # The source is the approved commit object, not the branch ref. A ref
        # can move between the check above and the push; the SHA the human
        # approved cannot.
        push_args += [cfg.remote_url, f"{approved_sha}:refs/heads/{branch}"]

        await _git(
            cfg,
            push_args,
            # Run from the repository's own checkout, never from the worktree
            # the agent writes to: git reads repository-local config from the
            # cwd's gitdir, and this command carries a credential.
            cwd=cfg.source_repo,
            extra_env=auth_env(token, cfg.remote_url),
            secrets=(token,),
            timeout_s=300,
        )

        existing = await find_open_pr(cfg, token, branch)
        if existing:
            pr = existing
            pr["created"] = False
        else:
            pr = await create_draft_pr(
                cfg,
                token,
                head_branch=branch,
                base_branch=cfg.base_branch,
                title=str(stored.get("title") or branch),
                body=str(stored.get("body") or ""),
            )
            pr["created"] = True
    except GitError as exc:
        forget_token(cfg)
        raise WorktreeError(f"git operation failed: {exc}")
    except GitHubError as exc:
        raise WorktreeError(str(exc))
    finally:
        token = None

    details = {
        "pushed_at": time.time(),
        "repository": cfg.source_repo,
        "repo": cfg.repo_slug,
        "branch": branch,
        "head_sha": live["head_sha"],
        "pull_request": pr,
    }
    approval_mod.mark_published(request_id, details, cfg=cfg)
    logger.info(
        "agent worktree: published repo=%s branch=%s sha=%s pr=%s",
        cfg.repo_slug, branch, live["head_sha"][:12], pr.get("number"),
    )
    return {"request_id": request_id, **details}


# ── approving in the browser ─────────────────────────────────────────────────

REVIEW_PATCH_MAX_BYTES = 300_000


async def review_patch(request_id: str, *, cfg: Optional[WorktreeConfig] = None) -> Dict[str, Any]:
    """What a publish request would push, for the person approving it: the
    approved commit's diff against the branch's base, read from its worktree."""
    cfg = cfg or load_config()
    stored = approval_mod.get_request(request_id, cfg=cfg)
    head = str(stored.get("head_sha") or "")
    if not is_valid_sha(head):
        raise WorktreeError("the request names no valid commit")
    rcfg, resolved, path = await _started(cfg, str(stored.get("branch") or ""), stored.get("repository"))
    base = await _base_ref(rcfg, path, resolved)
    res = await _git(rcfg, ["diff", "--no-color", "--no-ext-diff", "--no-textconv", f"{base}...{head}"],
                     cwd=path)
    text = res.stdout or ""
    return {"base": base, "head_sha": head, "patch": text[:REVIEW_PATCH_MAX_BYTES],
            "truncated": len(text) > REVIEW_PATCH_MAX_BYTES}


async def approve_and_publish(
    request_id: str,
    *,
    approver: str,
    allow_sensitive: bool = False,
    cfg: Optional[WorktreeConfig] = None,
) -> Dict[str, Any]:
    """Grant a request and publish it at once, for a person in the Odysseus UI.

    Same checks as the operator CLI's ``approve`` followed by the agent's
    ``publish``, but the one-time code never leaves this function: nothing
    the agent can read ever holds it. The grant is spent before the push, so a
    failed push or PR does not retry on a second approval: the request is
    marked failed and the agent has to call request_publish again.
    """
    cfg = cfg or load_config()
    stored = approval_mod.get_request(request_id, cfg=cfg)
    target = await repository_config(cfg, stored.get("repository"))
    blockers = publish_blockers(target)
    if blockers:
        raise WorktreeError("publishing is not available: " + "; ".join(blockers))
    try:
        code, _record = approval_mod.grant(request_id, allow_sensitive=allow_sensitive,
                                           granted_by=approver, cfg=cfg)
    except approval_mod.ApprovalError as exc:
        raise WorktreeError(str(exc))
    return await publish(request_id, code, cfg=cfg)


# ── cleanup ──────────────────────────────────────────────────────────────────

DISCARD_REF_PREFIX = "refs/odysseus/discarded/"
# Above this a file is left out of the recovery snapshot (and named in the
# result): a build artefact or model weight would bloat the object store.
DISCARD_FILE_CAP_BYTES = 5_000_000


def _snapshot_ref_name(branch: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-._") or "worktree"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{DISCARD_REF_PREFIX}{safe[:80]}-{stamp}"


async def _snapshot_worktree(
    cfg: WorktreeConfig, worktree: str, branch: str, dirty: List[str]
) -> Dict[str, Any]:
    """Commit everything in a dirty worktree to a ref nothing else points at.

    2026-10-02: the person told the admin agent to clean up worktrees "even if
    they have existing uncommitted data"; cleanup refused three of them and
    the old force path discards for good. A snapshot keeps the discard
    recoverable: it is built in a temporary index, so no branch, no stash and
    no index of the worktree is touched. ``add -A`` honours .gitignore, and
    oversized files are named, not stored.
    """
    tmp_dir = os.path.join(cfg.state_dir, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    index_file = os.path.join(tmp_dir, f"discard-{os.getpid()}-{time.time_ns()}.index")
    ident = {"GIT_AUTHOR_NAME": "Odysseus", "GIT_AUTHOR_EMAIL": "odysseus@localhost",
             "GIT_COMMITTER_NAME": "Odysseus", "GIT_COMMITTER_EMAIL": "odysseus@localhost"}
    env = {"GIT_INDEX_FILE": index_file}
    try:
        head = await _head_sha(cfg, worktree)
        await _git(cfg, ["read-tree", head], cwd=worktree, extra_env=env)
        await _git(cfg, ["add", "-A", "--", "."], cwd=worktree, extra_env=env, timeout_s=300)
        # Paths the status lists that are too large; for a rename or conflict
        # the last path is the one on disk.
        oversized: List[str] = []
        for line in dirty:
            name = line[3:].split(" -> ")[-1].strip().strip('"')
            full = os.path.join(worktree, name)
            try:
                if os.path.isfile(full) and os.path.getsize(full) > DISCARD_FILE_CAP_BYTES:
                    oversized.append(name)
            except OSError:
                continue
        for i in range(0, len(oversized), 100):
            # reset in the temporary index puts the HEAD entry back (or drops a new file).
            await _git(cfg, ["reset", "-q", head, "--", *oversized[i:i + 100]],
                       cwd=worktree, extra_env=env, check=False)
        tree = (await _git(cfg, ["write-tree"], cwd=worktree, extra_env=env)).stdout.strip()
        stamp = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
        message = (f"Discarded worktree snapshot\n\nbranch: {branch}\npath: {worktree}\n"
                   f"time: {stamp}\n")
        commit = (await _git(cfg, ["commit-tree", tree, "-p", head, "-m", message],
                             cwd=worktree, extra_env={**env, **ident})).stdout.strip()
        if not is_valid_sha(commit):
            raise WorktreeError("could not write the recovery snapshot; nothing was removed")
        ref = _snapshot_ref_name(branch)
        exists = await _git(cfg, ["rev-parse", "--verify", "--quiet", ref],
                            cwd=cfg.source_repo, check=False)
        if exists.ok:
            ref = f"{ref}-{commit[:7]}"
        await _git(cfg, ["update-ref", ref, commit], cwd=cfg.source_repo)
        changed = (await _git(cfg, ["diff-tree", "--no-commit-id", "--name-only", "-r", "-z",
                                    head, commit], cwd=worktree)).stdout
        return {"ref": ref, "commit": commit,
                "paths": len([p for p in changed.split("\0") if p]),
                "skipped_oversized": oversized}
    finally:
        try:
            os.unlink(index_file)
        except OSError:
            pass


async def cleanup(
    branch: str,
    *,
    cfg: Optional[WorktreeConfig] = None,
    repository: Optional[str] = None,
    discard_uncommitted: bool = False,
) -> Dict[str, Any]:
    """Detach a worktree and drop its branch, only when nothing would be lost.

    With ``discard_uncommitted`` a dirty worktree is snapshotted to
    refs/odysseus/discarded/* first, then force-removed. The caller must have
    checked that the person allowed it (worktree_tools does).

    Unlike the removed ``remove`` action (``git worktree remove --force``),
    this refuses a worktree with any modified or untracked file, runs a plain
    ``git worktree remove``, and deletes the agent branch only when another
    ref (a remote-tracking branch, another branch, a tag) still contains its
    tip — so the commits stay reachable. A branch with commits found nowhere
    else is kept and reported.
    """
    cfg = cfg or load_config()
    rcfg, resolved, path = await _locate(cfg, branch, repository)
    if not is_inside(path, cfg.worktree_root):
        raise WorktreeError("refusing to remove a path outside the worktree root")
    result: Dict[str, Any] = {
        "repository": rcfg.source_repo, "branch": resolved, "path": path,
        "worktree_removed": False, "branch_deleted": False,
    }
    try:
        with file_lock(_lock_path(cfg)):
            if os.path.lexists(path):
                if not _worktree_exists(path):
                    raise WorktreeError(
                        f"{path} exists but is not a git worktree; ask the operator to inspect it",
                        code="NOT_A_WORKTREE",
                    )
                _verify_membership(rcfg, path)
                current = await _current_branch(rcfg, path)
                if current and current != resolved:
                    raise WorktreeError(f"worktree is on {current}, expected {resolved}")
                dirty = await _dirty_entries(rcfg, path)
                if dirty and discard_uncommitted:
                    snap = await _snapshot_worktree(rcfg, path, resolved, dirty)
                    result["discarded_snapshot"] = {
                        "ref": snap["ref"], "commit": snap["commit"], "paths": snap["paths"]}
                    if snap["skipped_oversized"]:
                        result["discarded_snapshot"]["skipped_oversized"] = snap["skipped_oversized"][:50]
                    result["recovery_hint"] = (
                        f"git checkout -b <name> {snap['ref']} (in the repository) restores "
                        "the discarded changes")
                    await _git(rcfg, ["worktree", "remove", "--force", path],
                               cwd=rcfg.source_repo, timeout_s=120)
                    logger.warning(
                        "agent worktree: discarded uncommitted work repo=%s branch=%s paths=%d snapshot=%s",
                        rcfg.source_repo, resolved, snap["paths"], snap["ref"])
                    result["worktree_removed"] = not os.path.lexists(path)
                elif dirty:
                    shown = "; ".join(dirty[:20]) + (" ..." if len(dirty) > 20 else "")
                    raise WorktreeError(
                        f"worktree {path} has {len(dirty)} uncommitted or untracked path(s) "
                        f"({shown}); commit them or ask the user. Nothing was removed.",
                        code="WORKTREE_DIRTY",
                    )
                else:
                    # No --force: git itself refuses a dirty or locked worktree too.
                    await _git(rcfg, ["worktree", "remove", path], cwd=rcfg.source_repo, timeout_s=120)
                    result["worktree_removed"] = not os.path.lexists(path)
            else:
                listing = await _git(rcfg, ["worktree", "list", "--porcelain"],
                                     cwd=rcfg.source_repo, check=False)
                if any(
                    line.startswith("worktree ") and _same_path(line[len("worktree "):], path)
                    for line in listing.stdout.splitlines()
                ):
                    # Registered but the directory is gone: prune removes only
                    # registrations like this one.
                    await _git(rcfg, ["worktree", "prune"], cwd=rcfg.source_repo, check=False)
                    result["worktree_removed"] = True

            ref = f"refs/heads/{resolved}"
            tip = (await _git(rcfg, ["rev-parse", "--verify", "--quiet", ref],
                              cwd=rcfg.source_repo, check=False)).stdout.strip()
            if is_valid_sha(tip):
                checked_out = await _git(rcfg, ["worktree", "list", "--porcelain"],
                                         cwd=rcfg.source_repo, check=False)
                holders = (await _git(
                    rcfg, ["for-each-ref", "--format=%(refname)", "--contains", tip],
                    cwd=rcfg.source_repo, check=False,
                )).stdout.split()
                # The snapshot commit's parent is this tip, so its ref would make
                # every discarded branch look safely held elsewhere.
                holders = [h for h in holders if h != ref and h != "refs/stash"
                           and not h.startswith(DISCARD_REF_PREFIX)]
                if f"branch {ref}" in checked_out.stdout.splitlines():
                    result["branch_kept"] = "the branch is still checked out in another worktree"
                elif not holders:
                    result["branch_kept"] = (
                        "its commits are on no other ref; publish them (request_publish) or "
                        "ask the user before discarding them"
                    )
                else:
                    await _git(rcfg, ["update-ref", "-d", ref, tip], cwd=rcfg.source_repo)
                    result["branch_deleted"] = True
                    result["branch_tip"] = tip
                    result["tip_still_on"] = holders[:5]
            _drop_meta(rcfg, resolved)
    except LockBusy as exc:
        raise WorktreeError(str(exc))
    except GitError as exc:
        raise WorktreeError(str(exc))
    logger.info(
        "agent worktree: cleanup repo=%s branch=%s worktree_removed=%s branch_deleted=%s",
        rcfg.source_repo, resolved, result["worktree_removed"], result["branch_deleted"],
    )
    return result


async def remove_worktree(
    branch: str, *, cfg: Optional[WorktreeConfig] = None, repository: Optional[str] = None
) -> Dict[str, Any]:
    """Force-detach a worktree directory (operator use only; not an agent action).

    The branch itself is left alone. Discards uncommitted work — the agent
    tool refuses `remove` and offers `cleanup` instead.
    """
    cfg = cfg or load_config()
    rcfg, resolved, path = await _locate(cfg, branch, repository)
    if not is_inside(path, cfg.worktree_root):
        raise WorktreeError("refusing to remove a path outside the worktree root")
    try:
        with file_lock(_lock_path(cfg)):
            await _git(rcfg, ["worktree", "remove", "--force", path],
                       cwd=rcfg.source_repo, check=False, timeout_s=120)
            await _git(rcfg, ["worktree", "prune"], cwd=rcfg.source_repo, check=False)
    except LockBusy as exc:
        raise WorktreeError(str(exc))
    return {"removed": not os.path.exists(path), "path": path}
