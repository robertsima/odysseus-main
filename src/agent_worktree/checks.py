"""CI checks for an agent branch's pull request, read-only.

After a publish the worker used to watch CI by calling the GitHub MCP tools
over and over with `bash sleep 60` between calls: 27 calls in one hour on
2026-10-02, each a model round re-reading 30-60k tokens. One call here waits
inside the harness instead, and returns early when the person writes.

Two more incidents shaped the result: a hadolint failure on a Dockerfile the
branch never touched, and a dependency-review job that fails on every PR of
this repository, each cost the worker rounds and one unrelated commit. A
failing check that also fails on the PR's base is marked, so the worker names
it and moves on.

Nothing here writes to GitHub.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from src import agent_control
from src.agent_worktree import github as gh
from src.agent_worktree.config import WorktreeConfig

logger = logging.getLogger(__name__)

MAX_WAIT_S = 900
POLL_INTERVAL_S = 25.0
_monotonic = time.monotonic

_SKIPPED = frozenset({"skipped", "neutral"})


class NoOpenPullRequest(Exception):
    """The branch has no open pull request (yet)."""


def _bucket(run: Dict[str, Any]) -> str:
    if run.get("status") != "completed":
        return "in_progress"
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return "passed"
    if conclusion in _SKIPPED:
        return "skipped"
    # failure, timed_out, cancelled, action_required, startup_failure, stale
    return "failed" if conclusion else "in_progress"


def _row(run: Dict[str, Any]) -> Dict[str, Any]:
    state = run.get("conclusion") if run.get("status") == "completed" else run.get("status")
    return {"name": run.get("name"), "state": state, "url": run.get("url")}


def group_checks(runs: List[Dict[str, Any]], base_runs: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Group check runs by state; mark failures that also fail on the base.

    ``base_runs`` is None when the base was not looked up or the lookup
    failed; no failure is then marked as also failing there.
    """
    buckets: Dict[str, list] = {"in_progress": [], "passed": [], "failed": [], "skipped": []}
    for run in runs:
        buckets[_bucket(run)].append(run)
    base_failed = {r.get("name") for r in (base_runs or []) if _bucket(r) == "failed"}
    return {
        "in_progress": [_row(r) for r in buckets["in_progress"]],
        "passed": [r.get("name") for r in buckets["passed"]],
        "failed": [dict(_row(r), also_fails_on_base=r.get("name") in base_failed) for r in buckets["failed"]],
        "skipped": [r.get("name") for r in buckets["skipped"]],
    }


def _summary(grouped: Dict[str, Any], *, total: int, complete: bool, base_ref: str, base_checked: bool) -> str:
    if not total:
        return ("No check runs reported for this commit yet. CI may not have started: call checks "
                "again with wait_seconds. If none appear after a wait, this repository has no CI.")
    parts = []
    failed = grouped["failed"]
    if failed:
        mine = [f["name"] for f in failed if not f["also_fails_on_base"]]
        theirs = [f["name"] for f in failed if f["also_fails_on_base"]]
        text = f"{len(failed)} failed. Introduced by this branch: {', '.join(mine) if mine else 'none'}."
        if theirs:
            text += f" Already failing on {base_ref or 'the base'}: {', '.join(theirs)}."
        if not base_checked:
            text += " The base's checks could not be read, so treat each failure as possibly yours."
        parts.append(text)
    if grouped["in_progress"]:
        parts.append(f"{len(grouped['in_progress'])} still running.")
    if complete:
        parts.append(f"{len(grouped['passed'])} passed, {len(grouped['skipped'])} skipped.")
    return " ".join(parts)


async def _base_runs(rcfg: WorktreeConfig, token: str, base_ref: str, cache: Dict[str, Any]):
    """(base sha, runs) for the PR's base; runs is None when unavailable.

    One lookup per wait: the base's results do not change while the branch's
    own checks finish.
    """
    if "runs" in cache:
        return cache["sha"], cache["runs"]
    sha, runs = "", None
    try:
        sha = await gh.branch_head_sha(rcfg, token, base_ref)
        if sha:
            runs = await gh.pull_request_checks(rcfg, token, sha)
    except gh.GitHubError as exc:
        logger.info("agent worktree: base checks unavailable for %s: %s", base_ref, exc)
    cache.update(sha=sha, runs=runs)
    return sha, runs


async def branch_checks(
    rcfg: WorktreeConfig, branch: str, *, wait_seconds: float = 0, session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Check runs for ``branch``'s open PR; optionally wait until they finish.

    Raises ``NoOpenPullRequest`` when there is no PR and ``GitHubError`` when
    GitHub cannot be read on the first call.
    """
    try:
        wait = max(0.0, min(float(MAX_WAIT_S), float(wait_seconds or 0)))
    except (TypeError, ValueError):
        wait = 0.0
    token = await gh.resolve_token(rcfg)
    found = await gh.find_open_pr(rcfg, token, branch)
    if not found or not found.get("number"):
        raise NoOpenPullRequest(branch)
    # The same single-PR read the Workbench review view uses for head and base.
    pr = await gh.get_pull_request(rcfg, token, int(found["number"]))
    head_sha = str((pr.get("head") or {}).get("sha") or "")
    base_ref = str((pr.get("base") or {}).get("ref") or "")
    base_cache: Dict[str, Any] = {}

    started = _monotonic()
    deadline = started + wait
    steered = False
    poll_error = ""
    grouped: Dict[str, Any] = {"in_progress": [], "passed": [], "failed": [], "skipped": []}
    total = 0
    base_sha, base_runs = "", None
    first = True
    while True:
        try:
            runs = await gh.pull_request_checks(rcfg, token, head_sha)
        except gh.GitHubError as exc:
            if first:
                raise
            poll_error = str(exc)
            break
        first = False
        total = len(runs)
        # The base is read only once something has failed.
        if any(_bucket(r) == "failed" for r in runs):
            base_sha, base_runs = await _base_runs(rcfg, token, base_ref, base_cache)
        grouped = group_checks(runs, base_runs)
        remaining = deadline - _monotonic()
        if (total > 0 and not grouped["in_progress"]) or remaining <= 0:
            break
        if await agent_control.wait_or_steer(session_id, min(POLL_INTERVAL_S, remaining)):
            steered = True
            break

    complete = total > 0 and not grouped["in_progress"]
    out: Dict[str, Any] = {
        "exit_code": 0,
        "branch": branch,
        "pull_request": {"number": pr.get("number"), "url": pr.get("url"), "draft": pr.get("draft"),
                         "base": base_ref},
        "head_sha": head_sha,
        "counts": {k: len(v) for k, v in grouped.items()},
        **grouped,
        "complete": complete,
        "waited_s": round(_monotonic() - started, 1) if wait else 0.0,
        "summary": _summary(grouped, total=total, complete=complete, base_ref=base_ref,
                            base_checked=base_runs is not None or not grouped["failed"]),
    }
    if grouped["failed"]:
        out["base"] = {"ref": base_ref, "sha": base_sha, "checked": base_runs is not None}
    if poll_error:
        out["poll_error"] = f"A later poll failed ({poll_error}); this is the last result read."
    if steered:
        out["steer_note"] = agent_control.STEER_WAIT_NOTE
    return out
