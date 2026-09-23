"""Check a delegated task's prerequisites before a worker starts.

Workers used to start without a workspace and without file tools, work for a
while, and then report that they could not read the repository: the
2026-09-18 test runs have "no active workspace was set" and "source-inspection
tools were not attached" from workers that ran to a terminal state anyway.
This runs before launch and either configures the worker (binds a workspace,
attaches the file tools the task needs) or refuses to start it, with the call
that would fix it.

What a task needs is read from the launcher's explicit ``requires`` list
("workspace", "write", "read_only") and, failing that, from the task text.
The text heuristic only ever *attaches* things or blocks when repository work
is unmistakable; an ambiguous task starts with a warning instead.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

READ_TOOLS = ("get_workspace", "read_file", "grep", "glob", "ls")
WRITE_TOOLS = ("write_file", "edit_file", "apply_patch")
REQUIREMENTS = ("workspace", "write", "read_only")

# Unmistakable repository work: files, code, tests, git.
_REPO_RE = re.compile(
    r"\b(?:repo|repos|repository|repositories|codebase|source code|source files?|source tree|"
    r"(?:the|this|our) code|in the code|git|pull requests?|"
    r"pytest|unit tests?|test suite|run (?:the )?tests|grep|working tree|worktree|"
    r"read[- ]only audit of the (?:code|source|repo\w*)|source[- ]inspection)\b"
    r"|\b(?:src|tests|static|routes|services|core)/[\w./-]+"
    r"|\b[\w-]+\.(?:py|js|mjs|ts|tsx|jsx|go|rs|java|rb|php|css|html|toml|ya?ml|json)\b",
    re.IGNORECASE,
)
# Asks for changes to files. Read-only phrasing wins over it.
_WRITE_RE = re.compile(
    r"\b(?:fix|implement|refactor|edit|modify|change|patch|rewrite|update|add|remove|delete|rename|"
    r"create|write)\b[^.\n]{0,60}\b(?:code|files?|function|class|module|tests?|bug|script|config|"
    r"repo\w*|source|endpoint|feature|handler)\b",
    re.IGNORECASE,
)
_READ_ONLY_RE = re.compile(
    r"\bread[- ]only\b|\bdo not (?:edit|modify|change|write)\b|\bdon'?t (?:edit|modify|change|write)\b|"
    r"\bno (?:edits|writes|changes)\b|\bwithout (?:editing|changing|modifying)\b",
    re.IGNORECASE,
)


def task_needs_workspace(task: str) -> bool:
    return bool(_REPO_RE.search(task or ""))


def task_needs_write(task: str) -> bool:
    text = task or ""
    return bool(_WRITE_RE.search(text)) and not _READ_ONLY_RE.search(text)


def known_checkouts() -> List[str]:
    """Paths of the Git checkouts agents may work in (get_workspace lists
    the same ones)."""
    try:
        from src.agent_tools.claude_code_tools import discover_repositories

        return [str(repo["path"]) for repo in discover_repositories() if repo.get("path")]
    except Exception:
        return []


def _checkout_named_in(task: str, checkouts: Iterable[str]) -> Optional[str]:
    """The one checkout whose folder name the task mentions, if exactly one."""
    text = (task or "").casefold()
    named = []
    for path in checkouts:
        name = os.path.basename(os.path.normpath(path)).casefold()
        if name and re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", text):
            named.append(path)
    return named[0] if len(named) == 1 else None


@dataclass
class Preflight:
    workspace: Optional[str] = None
    workspace_source: str = ""
    needs_workspace: bool = False
    needs_write: bool = False
    forced_tools: Set[str] = field(default_factory=set)
    problems: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "workspace": self.workspace,
            "workspace_source": self.workspace_source or None,
            "needs_workspace": self.needs_workspace,
            "needs_write": self.needs_write,
            "attached_tools": sorted(self.forced_tools),
        }
        if self.warnings:
            out["warnings"] = list(self.warnings)
        return out

    def blocked_payload(self) -> Dict[str, Any]:
        first = self.problems[0]
        return {
            "error": "Worker not started: " + "; ".join(p["message"] for p in self.problems),
            "status": "blocked",
            "code": first["code"],
            "next_action": first.get("next_action"),
            "problems": list(self.problems),
            "preflight": self.summary(),
            "exit_code": 1,
        }


class WorkerBlocked(ValueError):
    """A launch refused by preflight. ``str()`` is the readable reason;
    ``payload`` carries the codes and the next call."""

    def __init__(self, preflight: Preflight):
        self.preflight = preflight
        self.payload = preflight.blocked_payload()
        super().__init__(self.payload["error"])


def run_preflight(
    task: str,
    *,
    explicit_workspace: Optional[str] = None,
    inherited_workspace: Optional[str] = None,
    unavailable_tools: Iterable[str] = (),
    requires: Iterable[str] = (),
) -> Preflight:
    """Decide the worker's workspace and file tools, or why it cannot start.

    ``unavailable_tools`` is everything the worker could not call (disabled
    for it, for its owner, or outside its loadout's allowlist).
    """
    from src.tool_execution import vet_workspace

    wanted = {str(r).strip().lower() for r in (requires or ()) if str(r).strip()}
    unknown = wanted - set(REQUIREMENTS)
    pf = Preflight()
    if unknown:
        pf.problems.append({
            "code": "UNKNOWN_REQUIREMENT",
            "message": f"unknown requires value(s) {sorted(unknown)}; use {list(REQUIREMENTS)}",
        })
        return pf
    read_only = "read_only" in wanted
    pf.needs_write = "write" in wanted or (not read_only and task_needs_write(task))
    pf.needs_workspace = "workspace" in wanted or pf.needs_write or task_needs_workspace(task)
    unavailable = set(unavailable_tools or ())

    if explicit_workspace:
        vetted = vet_workspace(explicit_workspace)
        if vetted is None:
            pf.problems.append({
                "code": "WORKSPACE_INVALID",
                "message": (f"workspace {explicit_workspace!r} is not a directory agents may use "
                            "(missing, sensitive, or application state)"),
                "candidates": known_checkouts(),
                "required_fields": ["workspace"],
            })
            return pf
        pf.workspace, pf.workspace_source = vetted, "requested"
    elif inherited_workspace and vet_workspace(inherited_workspace):
        pf.workspace, pf.workspace_source = vet_workspace(inherited_workspace), "parent chat"
    elif pf.needs_workspace:
        checkouts = [c for c in known_checkouts() if vet_workspace(c)]
        chosen = _checkout_named_in(task, checkouts) or (checkouts[0] if len(checkouts) == 1 else None)
        if chosen:
            pf.workspace = vet_workspace(chosen)
            pf.workspace_source = "named in the task" if len(checkouts) > 1 else "only checkout"
        else:
            pf.problems.append({
                "code": "WORKSPACE_REQUIRED",
                "message": ("the task works on a repository but no workspace is set; "
                            + ("pass workspace as one of: " + ", ".join(checkouts[:8]) if checkouts else
                               "no checkout is available under the configured repository roots")),
                "candidates": checkouts,
                "required_fields": ["workspace"],
                "next_action": ({"retry_with": {"workspace": checkouts[0]}, "choose_from": checkouts}
                                if checkouts else None),
            })
            return pf

    if pf.workspace and not os.access(pf.workspace, os.R_OK | os.X_OK):
        pf.problems.append({
            "code": "WORKSPACE_UNREADABLE",
            "message": f"workspace {pf.workspace!r} is not readable by the server",
        })
        return pf

    if pf.workspace:
        missing_read = [t for t in ("read_file", "grep", "ls") if t in unavailable]
        if pf.needs_workspace and missing_read:
            pf.problems.append({
                "code": "TOOLS_UNAVAILABLE",
                "message": f"the worker cannot use {', '.join(missing_read)}, which reading the repository needs",
                "next_action": {"enable_tools": missing_read},
            })
        pf.forced_tools.update(t for t in READ_TOOLS if t not in unavailable)
    if pf.needs_write:
        writable = [t for t in WRITE_TOOLS if t not in unavailable]
        if not writable:
            pf.problems.append({
                "code": "WRITE_NOT_ALLOWED",
                "message": "the task asks for changes but the worker has no file-writing tool",
                "next_action": {"enable_tools": list(WRITE_TOOLS),
                                "or": "restate the task as read-only (requires: ['read_only'])"},
            })
        pf.forced_tools.update(writable)
    if not pf.workspace and not pf.needs_workspace:
        pf.warnings.append("no workspace is attached, so the worker cannot read or change files")
    return pf


def worker_unavailable_tools(owner: Optional[str], profile: Optional[Dict[str, Any]] = None,
                             extra: Iterable[str] = ()) -> Set[str]:
    """Tools the worker could not call: the owner's baseline (global disabled
    tools and privileges), the profile's own switches, and anything outside a
    selected-tools allowlist."""
    unavailable: Set[str] = set(extra or ())
    try:
        from src.tool_security import owner_baseline_disabled_tools

        unavailable |= set(owner_baseline_disabled_tools(owner) or ())
    except Exception:
        pass
    if profile:
        unavailable |= set(profile.get("disabled_tools") or ())
        if profile.get("tool_access") == "selected":
            allowed = set(profile.get("enabled_tools") or ())
            unavailable |= {t for t in (*READ_TOOLS, *WRITE_TOOLS) if t not in allowed}
        elif profile.get("tool_access") == "none":
            unavailable |= set(READ_TOOLS) | set(WRITE_TOOLS)
    return unavailable


def record_blocked(session_id: Optional[str], owner: Optional[str], task: str, pf: Preflight) -> Optional[str]:
    """Put the refused launch on the chat's activity feed as a blocked run,
    so the Agents panel shows it with its reason."""
    if not session_id:
        return None
    from src import agent_activity as activity

    run_id = activity.run_started(session_id, "session", f"Worker · {task[:80]}", owner=owner,
                                  data={"mode": "agent"}, detail=task[:1500])
    activity.run_finished(session_id, "session", run_id,
                          f"Worker blocked before start: {pf.problems[0]['message']}"[:300],
                          status="blocked", owner=owner,
                          data={"error": "; ".join(p["message"] for p in pf.problems)[:400]})
    return run_id
