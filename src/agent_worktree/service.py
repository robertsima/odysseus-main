"""Operations the agent tool and the operator CLI both call.

The shape of the flow, and why each step exists:

1. ``ensure_worktree`` gives the agent one persistent checkout it owns. It is a
   real ``git worktree`` of the operator's repository, on a branch inside the
   ``agent/odysseus/`` namespace, living under a configured root that every path
   is re-checked against.
2. The agent edits and tests there with its normal tools, and commits through
   ``commit`` (fixed bot identity, argv-only git).
3. ``request_publish`` freezes the change: it records the exact repo, branch,
   head SHA, file list and sensitive-path classification, and returns them for a
   human to read. It publishes nothing.
4. A human runs the CLI, sees the file list, and grants a short-lived single-use
   approval code.
5. ``publish`` spends that code — re-verifying every bound fact against the live
   worktree, not against what the request claimed — then pushes and opens a
   **draft** PR.

Every step re-derives state from git rather than trusting a stored value, so a
tampered state file cannot substitute a different change for the approved one.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any, Dict, List, Optional

from src.agent_worktree import approval as approval_mod
from src.agent_worktree.config import BRANCH_PREFIX, WorktreeConfig, load_config, publish_blockers
from src.agent_worktree.gitcmd import GitError, auth_env, git_path, run_git
from src.agent_worktree.locking import LockBusy, file_lock
from src.agent_worktree import sensitive as sensitive_mod
from src.agent_worktree.validation import (
    branch_leaf_from_name,
    is_inside,
    is_valid_sha,
    safe_worktree_path,
    validate_agent_branch,
)

logger = logging.getLogger(__name__)

MAX_CHANGED_FILES = 500


class WorktreeError(RuntimeError):
    """An operation could not be completed safely."""


def _lock_path(cfg: WorktreeConfig) -> str:
    return os.path.join(cfg.state_dir, "locks", "worktree.lock")


def files_digest(paths: List[str]) -> str:
    hasher = hashlib.sha256()
    for path in sorted(set(paths)):
        hasher.update(path.encode("utf-8") + b"\n")
    return hasher.hexdigest()


async def _head_sha(worktree: str) -> str:
    res = await run_git(["rev-parse", "HEAD"], cwd=worktree)
    sha = res.stdout.strip()
    if not is_valid_sha(sha):
        raise WorktreeError("could not read a valid HEAD commit")
    return sha


async def _current_branch(worktree: str) -> str:
    res = await run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=worktree, check=False)
    return res.stdout.strip() if res.ok else ""


async def _is_dirty(worktree: str) -> bool:
    res = await run_git(["status", "--porcelain"], cwd=worktree)
    return bool(res.stdout.strip())


async def _base_ref(cfg: WorktreeConfig, worktree: str) -> str:
    """Ref to diff against: the tracked remote base if present, else local."""
    for candidate in (f"origin/{cfg.base_branch}", cfg.base_branch):
        res = await run_git(
            ["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"],
            cwd=worktree,
            check=False,
        )
        if res.ok and res.stdout.strip():
            return candidate
    raise WorktreeError(
        f"base branch {cfg.base_branch!r} not found in the worktree; fetch it first"
    )


async def _changed_files(cfg: WorktreeConfig, worktree: str) -> List[str]:
    """Files the branch changes relative to its merge base with the base branch."""
    base = await _base_ref(cfg, worktree)
    res = await run_git(["diff", "--name-only", f"{base}...HEAD"], cwd=worktree)
    files = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    if len(files) > MAX_CHANGED_FILES:
        raise WorktreeError(
            f"change touches {len(files)} files (limit {MAX_CHANGED_FILES}); "
            "split it into smaller branches"
        )
    return files


def _worktree_dir(cfg: WorktreeConfig, branch: str) -> str:
    path = safe_worktree_path(cfg.worktree_root, branch, BRANCH_PREFIX)
    if not path:
        raise WorktreeError(f"branch {branch!r} is not a valid agent branch")
    return path


async def ensure_worktree(
    name: str, *, cfg: Optional[WorktreeConfig] = None
) -> Dict[str, Any]:
    """Create or reuse the persistent worktree for one agent branch.

    Editing and testing do not require publishing to be enabled — the isolation
    is useful on its own — so this deliberately does not check the publish flag.
    """
    cfg = cfg or load_config()
    if not git_path():
        raise WorktreeError("git is not installed or not on PATH")
    branch = branch_leaf_from_name(name, BRANCH_PREFIX)
    if not branch:
        raise WorktreeError(
            "worktree name must form a valid branch under " + BRANCH_PREFIX
        )
    if not os.path.exists(os.path.join(cfg.source_repo, ".git")):
        raise WorktreeError(f"source repository {cfg.source_repo} is not a git checkout")

    path = _worktree_dir(cfg, branch)
    os.makedirs(cfg.worktree_root, exist_ok=True)

    try:
        with file_lock(_lock_path(cfg)):
            # Reuse an existing, healthy worktree.
            if os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(
                os.path.join(path, ".git")
            ):
                current = await _current_branch(path)
                if current != branch:
                    raise WorktreeError(
                        f"worktree {path} is on {current or 'a detached HEAD'}, "
                        f"expected {branch}"
                    )
                return await status(branch, cfg=cfg)

            # Stale registration from a deleted directory would block `add`.
            await run_git(["worktree", "prune"], cwd=cfg.source_repo, check=False)

            branch_exists = (
                await run_git(
                    ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                    cwd=cfg.source_repo,
                    check=False,
                )
            ).stdout.strip()

            if branch_exists:
                args = ["worktree", "add", path, branch]
            else:
                start_point = (
                    f"origin/{cfg.base_branch}"
                    if (
                        await run_git(
                            [
                                "rev-parse",
                                "--verify",
                                "--quiet",
                                f"refs/remotes/origin/{cfg.base_branch}^{{commit}}",
                            ],
                            cwd=cfg.source_repo,
                            check=False,
                        )
                    ).stdout.strip()
                    else cfg.base_branch
                )
                args = ["worktree", "add", "-b", branch, path, start_point]
            await run_git(args, cwd=cfg.source_repo, timeout_s=300)
    except LockBusy as exc:
        raise WorktreeError(str(exc))
    except GitError as exc:
        raise WorktreeError(str(exc))

    logger.info("agent worktree: ready branch=%s path=%s", branch, path)
    return await status(branch, cfg=cfg)


async def status(
    branch: Optional[str] = None, *, cfg: Optional[WorktreeConfig] = None
) -> Dict[str, Any]:
    """Configuration and worktree state. Contains no credential material."""
    cfg = cfg or load_config()
    out: Dict[str, Any] = {
        "publish_enabled": cfg.publish_enabled,
        "repo": cfg.repo_slug or None,
        "base_branch": cfg.base_branch,
        "branch_prefix": BRANCH_PREFIX,
        "worktree_root": cfg.worktree_root,
        "approval_ttl_seconds": cfg.approval_ttl_s,
        "credential": (
            "github_app" if cfg.has_github_app
            else ("fallback_token" if cfg.has_any_credential else None)
        ),
        "publish_blockers": publish_blockers(cfg),
        "worktrees": [],
    }
    try:
        names = sorted(os.listdir(cfg.worktree_root))
    except OSError:
        names = []
    for entry in names:
        path = os.path.join(cfg.worktree_root, entry)
        if not os.path.isdir(path) or not is_inside(path, cfg.worktree_root):
            continue
        try:
            entry_branch = await _current_branch(path)
            info = {
                "path": path,
                "branch": entry_branch,
                "head_sha": await _head_sha(path) if entry_branch else None,
                "dirty": await _is_dirty(path),
            }
        except (GitError, WorktreeError):
            info = {"path": path, "branch": None, "head_sha": None, "dirty": None}
        out["worktrees"].append(info)

    if branch:
        resolved = validate_agent_branch(branch, BRANCH_PREFIX) or branch_leaf_from_name(
            branch, BRANCH_PREFIX
        )
        if resolved:
            path = _worktree_dir(cfg, resolved)
            out["branch"] = resolved
            out["path"] = path
            out["exists"] = os.path.exists(os.path.join(path, ".git"))
            if out["exists"]:
                out["head_sha"] = await _head_sha(path)
                out["dirty"] = await _is_dirty(path)
    return out


async def commit(
    branch: str, message: str, *, cfg: Optional[WorktreeConfig] = None
) -> Dict[str, Any]:
    """Stage everything in the worktree and commit it under the bot identity."""
    cfg = cfg or load_config()
    resolved = branch_leaf_from_name(branch, BRANCH_PREFIX)
    if not resolved:
        raise WorktreeError("invalid agent branch")
    text = (message or "").strip()
    if not text:
        raise WorktreeError("a commit message is required")
    path = _worktree_dir(cfg, resolved)
    if not os.path.exists(os.path.join(path, ".git")):
        raise WorktreeError("worktree does not exist yet; start it first")

    try:
        with file_lock(_lock_path(cfg)):
            current = await _current_branch(path)
            if current != resolved:
                raise WorktreeError(
                    f"worktree is on {current or 'a detached HEAD'}, expected {resolved}"
                )
            if not await _is_dirty(path):
                return {"committed": False, "reason": "nothing to commit",
                        "head_sha": await _head_sha(path)}
            await run_git(["add", "-A", "--", "."], cwd=path)
            # -- separates the message from anything that could look like a flag.
            await run_git(["commit", "-m", text[:4000], "--no-verify"], cwd=path,
                          timeout_s=300)
            return {"committed": True, "head_sha": await _head_sha(path)}
    except LockBusy as exc:
        raise WorktreeError(str(exc))
    except GitError as exc:
        raise WorktreeError(str(exc))


async def diff_summary(
    branch: str, *, cfg: Optional[WorktreeConfig] = None
) -> Dict[str, Any]:
    """Changed files plus the sensitive-path classification for them."""
    cfg = cfg or load_config()
    resolved = branch_leaf_from_name(branch, BRANCH_PREFIX)
    if not resolved:
        raise WorktreeError("invalid agent branch")
    path = _worktree_dir(cfg, resolved)
    if not os.path.exists(os.path.join(path, ".git")):
        raise WorktreeError("worktree does not exist yet; start it first")
    try:
        # The summary reports HEAD, and publish pushes the branch. If those two
        # have been separated — a detached HEAD parked on the reviewed commit
        # while the branch ref points at something else — the operator would
        # approve one commit and a different one would be published.
        current = await _current_branch(path)
        if current != resolved:
            raise WorktreeError(
                f"worktree is on {current or 'a detached HEAD'}, expected {resolved}"
            )
        files = await _changed_files(cfg, path)
        findings = sensitive_mod.classify(files)
        return {
            "branch": resolved,
            "head_sha": await _head_sha(path),
            "dirty": await _is_dirty(path),
            "changed_files": files,
            "files_digest": files_digest(files),
            "sensitive": findings,
            "sensitive_digest": sensitive_mod.digest(findings),
            "sensitive_summary": sensitive_mod.summarize(findings),
        }
    except GitError as exc:
        raise WorktreeError(str(exc))


async def _remote_head(cfg: WorktreeConfig, branch: str, token: Optional[str]) -> Optional[str]:
    """Current SHA of the branch on the remote, or None when it is absent.

    Raises on a lookup failure so the caller can record "unknown" explicitly
    rather than mistaking a network error for an absent branch.
    """
    res = await run_git(
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
) -> Dict[str, Any]:
    """Freeze the change and record an approval request. Publishes nothing."""
    cfg = cfg or load_config()
    blockers = publish_blockers(cfg)
    if blockers:
        raise WorktreeError("publishing is not available: " + "; ".join(blockers))

    summary = await diff_summary(branch, cfg=cfg)
    if summary["dirty"]:
        raise WorktreeError(
            "worktree has uncommitted changes; commit them so the approval "
            "covers exactly what would be pushed"
        )
    if not summary["changed_files"]:
        raise WorktreeError("branch has no changes relative to the base branch")

    from src.agent_worktree.github import GitHubError, resolve_token

    try:
        token = await resolve_token(cfg)
        remote_head = await _remote_head(cfg, summary["branch"], token)
        remote_state = remote_head or "absent"
    except (GitHubError, GitError) as exc:
        # Recorded as unknown: publish then refuses a force-push and does a
        # plain push, which fails loudly if the remote branch moved.
        logger.warning("agent worktree: remote head lookup failed: %s", exc)
        remote_state = "unknown"
    finally:
        token = None

    record = approval_mod.create_request(
        repo=cfg.repo_slug,
        branch=summary["branch"],
        head_sha=summary["head_sha"],
        base_branch=cfg.base_branch,
        title=(title or summary["branch"])[:250],
        body=(body or "")[:60000],
        changed_files=summary["changed_files"],
        files_digest=summary["files_digest"],
        sensitive=summary["sensitive"],
        sensitive_digest=summary["sensitive_digest"],
        remote_state=remote_state,
        requested_by=requested_by,
        cfg=cfg,
    )
    view = approval_mod.public_view(record)
    view["remote_state"] = remote_state
    view["sensitive_summary"] = summary["sensitive_summary"]
    view["next_step"] = (
        "A human must approve on the host: "
        f"scripts/odysseus-agent-worktree approve {record['id']}"
        + (" --allow-sensitive" if summary["sensitive"] else "")
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
    blockers = publish_blockers(cfg)
    if blockers:
        raise WorktreeError("publishing is not available: " + "; ".join(blockers))

    stored = approval_mod.get_request(request_id, cfg=cfg)
    branch = validate_agent_branch(stored.get("branch") or "", BRANCH_PREFIX)
    if not branch:
        raise WorktreeError("stored request does not name a valid agent branch")
    if stored.get("repo") != cfg.repo_slug:
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
        return await _publish_locked(cfg, request_id, approval_code, stored, branch)
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
    live = await diff_summary(branch, cfg=cfg)
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

        push_args = ["push"]
        if expected_remote not in ("unknown", "absent"):
            # Bound to the exact remote SHA the operator approved against, so a
            # concurrent push cannot be silently overwritten.
            push_args.append(f"--force-with-lease=refs/heads/{branch}:{expected_remote}")
        # The source is the approved commit object, not the branch ref. A ref
        # can move between the check above and the push; the SHA the human
        # approved cannot.
        push_args += [cfg.remote_url, f"{approved_sha}:refs/heads/{branch}"]

        await run_git(
            push_args,
            # Run from the operator's checkout, never from the worktree the
            # agent writes to: git reads repository-local config from the cwd's
            # gitdir, and this command carries a credential.
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
        "branch": branch,
        "head_sha": live["head_sha"],
        "pull_request": pr,
    }
    approval_mod.mark_published(request_id, details, cfg=cfg)
    logger.info(
        "agent worktree: published branch=%s sha=%s pr=%s",
        branch, live["head_sha"][:12], pr.get("number"),
    )
    return {"request_id": request_id, **details}


async def remove_worktree(
    branch: str, *, cfg: Optional[WorktreeConfig] = None
) -> Dict[str, Any]:
    """Detach a worktree directory. The branch itself is left alone."""
    cfg = cfg or load_config()
    resolved = branch_leaf_from_name(branch, BRANCH_PREFIX)
    if not resolved:
        raise WorktreeError("invalid agent branch")
    path = _worktree_dir(cfg, resolved)
    if not is_inside(path, cfg.worktree_root):
        raise WorktreeError("refusing to remove a path outside the worktree root")
    try:
        with file_lock(_lock_path(cfg)):
            await run_git(["worktree", "remove", "--force", path],
                          cwd=cfg.source_repo, check=False, timeout_s=120)
            await run_git(["worktree", "prune"], cwd=cfg.source_repo, check=False)
    except LockBusy as exc:
        raise WorktreeError(str(exc))
    return {"removed": not os.path.exists(path), "path": path}
