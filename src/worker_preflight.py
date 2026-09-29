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

# Unmistakable LOCAL repository work: code, tests, git, paths, file names.
#
# The bare words "repo"/"repository"/"repositories" are deliberately not in
# here. 2026-09-28: a web-research task ("... recent technical sources,
# repositories, issue trackers ...") matched "repositories", so preflight
# treated it as repository work and refused to start a web_search-only worker
# with "the worker cannot use read_file, grep, ls, which reading the
# repository needs". Research about repositories on GitHub is web research;
# only a repository the task places HERE (see _LOCAL_REPO_RE) needs a
# workspace.
_REPO_RE = re.compile(
    r"\b(?:codebase|source code|source files?|source tree|"
    r"(?:the|this|our) code|in the code|git(?![-\u2010\u2011]|\s*like\b)|"
    r"pytest|unit tests?|test suite|run (?:the )?tests|grep|working tree|worktree|"
    r"read[- ]only audit of the (?:code|source|repo\w*)|source[- ]inspection)\b"
    r"|\b(?:src|tests|static|routes|services|core)/[\w./-]+",
    re.IGNORECASE,
)
# A file name. Checked separately so a URL's last segment (".../page.html")
# and framework names ("Node.js") do not count as files in a checkout.
_FILE_NAME_RE = re.compile(
    r"(?<![/\w.])[\w-]+\.(?:py|js|mjs|ts|tsx|jsx|go|rs|java|rb|php|css|html|toml|ya?ml|json)\b",
    re.IGNORECASE,
)
_NOT_A_FILE_RE = re.compile(
    r"^(?:node|next|nuxt|vue|react|three|d3|express|chart|ember|backbone|angular|alpine|p5|tensorflow|"
    r"brain|ml5|socket|deno|bun|solid|svelte)\.js$",
    re.IGNORECASE,
)
# A path on this machine: /app/data/..., ./src/x, C:\dev\repo. At least two
# segments, and not the path part of a URL (preceded by a host name or "//").
_LOCAL_PATH_RE = re.compile(
    r"(?<![\w:/.])(?:\.{0,2}/)(?:[\w.-]+/)+[\w.-]*"
    r"|\b[A-Za-z]:[\\/][\w .\\/-]+",
)
# "repo"/"repository" words, and pull requests. Each counts as local
# repository work only when the task places it here (_is_local_repo_mention).
# "checkout" is not here: "the checkout flow" is as likely a shop as a clone,
# and a checkout on this machine is named by its path.
_REPO_WORD_RE = re.compile(
    r"\b(?:repo|repos|repository|repositories|pull requests?)\b",
    re.IGNORECASE,
)
# Determiners that put a repository in THIS workspace. "the"/"that" only for a
# singular repository: "the repositories behind these papers" is not a local
# checkout, "the repository" usually is.
_LOCAL_DETERMINER_RE = re.compile(
    r"\b(?:this|our|my|your|local|same|odysseus(?:'s)?)\s+(?:[\w-]+\s+)?$", re.IGNORECASE)
_SINGULAR_DETERMINER_RE = re.compile(r"\b(?:the|that|its)\s+(?:[\w-]+\s+)?$", re.IGNORECASE)
# Wording that puts a repository on the web rather than in a workspace.
_WEB_CONTEXT_RE = re.compile(
    r"\b(?:github|gitlab|bitbucket|codeberg|sourceforge|hugging\s?face|arxiv|online|internet|"
    r"on the web|the web|web (?:research|search|sources?)|open[- ]source|public|third[- ]party|"
    r"external|issue trackers?|papers?|blogs?|articles?|websites?)\b",
    re.IGNORECASE,
)
_WEB_WINDOW = 80
# Asks for changes to files. Read-only phrasing wins over it.
_WRITE_RE = re.compile(
    r"\b(?:fix|implement|refactor|edit|modify|change|patch|rewrite|update|add|remove|delete|rename|"
    r"create|write)\b[^.\n]{0,60}\b(?:code|files?|function|class|module|tests?|bug|script|config|"
    r"(?P<repo>repo\w*)|source|endpoint|feature|handler)\b",
    re.IGNORECASE,
)
_READ_ONLY_RE = re.compile(
    r"\bread[- ]only\b|\bdo not (?:edit|modify|change|write)\b|\bdon'?t (?:edit|modify|change|write)\b|"
    r"\bno (?:edits|writes|changes)\b|\bwithout (?:editing|changing|modifying)\b",
    re.IGNORECASE,
)


def _web_context(text: str, start: int, end: int) -> bool:
    """Whether the words around ``text[start:end]`` place it on the web."""
    window = text[max(0, start - _WEB_WINDOW):end + _WEB_WINDOW]
    return bool(_WEB_CONTEXT_RE.search(window))


def _is_local_repo_mention(text: str, match: "re.Match[str]") -> bool:
    """A repo/repository/pull-request word that means one here.

    Local when a determiner puts it in this workspace ("this repo", "our
    repositories", "the repository") or a path follows it ("repository
    /app/data/..."); never when the surrounding words put it on the web
    ("GitHub repositories", "repositories, issue trackers").
    """
    word = match.group(0).casefold()
    if _web_context(text, match.start(), match.end()):
        return False
    if word.startswith("pull request"):
        return True
    before = text[max(0, match.start() - 40):match.start()]
    if _LOCAL_DETERMINER_RE.search(before):
        return True
    plural = word in ("repos", "repositories")
    if not plural and _SINGULAR_DETERMINER_RE.search(before):
        return True
    after = text[match.end():match.end() + 60]
    return bool(re.match(r"\s+(?:at\s+|in\s+|root\s+)?(?:`|\.{0,2}/|[A-Za-z]:[\\/])", after))


def _local_repo_signal(text: str) -> bool:
    if _REPO_RE.search(text) or _LOCAL_PATH_RE.search(text):
        return True
    for match in _FILE_NAME_RE.finditer(text):
        if not _NOT_A_FILE_RE.match(match.group(0)):
            return True
    return any(_is_local_repo_mention(text, m) for m in _REPO_WORD_RE.finditer(text))


def task_needs_workspace(task: str) -> bool:
    """Whether the task is repository work on a checkout here.

    Signals: code/test/git wording, a path on this machine, a file name, or a
    repository the task places here ("this repo", "the repository", "our
    repositories"). Repositories and issue trackers named as web sources are
    research, not a workspace.
    """
    return _local_repo_signal(task or "")


def task_needs_write(task: str) -> bool:
    text = task or ""
    if _READ_ONLY_RE.search(text):
        return False
    for match in _WRITE_RE.finditer(text):
        # "write a summary of the repositories on GitHub" asks for prose, not
        # a change to a checkout: a repository as the object counts only when
        # the task has a local repository in it.
        if match.group("repo") and not _local_repo_signal(text):
            continue
        return True
    return False


# Tools a worker can read a note or document with.
DOCUMENT_READ_TOOLS = ("search_documents", "read_file", "manage_documents", "manage_notes",
                       "vault_search", "vault_get")
# A task that names a note, document or vault file the worker is meant to read.
_DOCUMENT_RE = re.compile(
    r"\b(?:the|this|that|my|our|your)\s+(?:[\w'’&-]+\s+){0,5}?"
    r"(?<!release )(?<!patch )(?:note|notes|document|documents)\b"
    r"|\b(?:in|from)\s+(?:my|the|our)\s+(?:vault|notes|documents)\b"
    r"|\bvault\b|\bobsidian\b"
    r"|(?<![/\w.])[\w-]+\.md\b",
    re.IGNORECASE,
)


def task_names_document(task: str) -> bool:
    return bool(_DOCUMENT_RE.search(task or ""))


def document_access_warning(task: str, profile: Optional[Dict[str, Any]]) -> Optional[str]:
    """A warning when the task names a note/document the worker cannot read.

    2026-09-28: "Creative Agent Memory Researcher" (tools=[web_search]) was
    asked to "reconcile against the AI Mind note" and could only say it had
    no way to read it. Not a hard block: the parent may have pasted the
    content into the task.
    """
    if not profile or not task_names_document(task):
        return None
    access = profile.get("tool_access") or "all"
    if access == "all":
        return None
    enabled = set(profile.get("enabled_tools") or []) - set(profile.get("disabled_tools") or [])
    if access == "selected" and enabled & set(DOCUMENT_READ_TOOLS):
        return None
    return (
        f"the task names a note or document, but loadout {profile.get('name', '?')!r} has no tool that "
        "reads documents (" + ", ".join(DOCUMENT_READ_TOOLS[:3]) + "). Put the relevant content in the "
        "task itself, or start it with extra_tools=[\"search_documents\"] for this run."
    )


def known_checkouts() -> List[str]:
    """Paths of the Git checkouts agents may work in (get_workspace lists
    the same ones)."""
    try:
        from src.agent_tools.claude_code_tools import discover_repositories

        return [str(repo["path"]) for repo in discover_repositories() if repo.get("path")]
    except Exception:
        return []


def _origin_repo_name(path: str) -> str:
    """The repository name of a checkout's GitHub ``origin`` ("" if none).

    People name a project by its repository, not its folder: on 2026-09-29 the
    task said "Umni" and the checkout was /app/data/development/dog-trainer.
    """
    try:
        from src.agent_worktree.service import _origin_slug

        return (_origin_slug(path) or "").rsplit("/", 1)[-1]
    except Exception:
        return ""


def _is_linked_worktree(path: str) -> bool:
    """A linked worktree's ``.git`` is a pointer file, a main checkout's a folder."""
    return os.path.isfile(os.path.join(path, ".git"))


def _checkout_named_in(task: str, checkouts: Iterable[str]) -> Optional[str]:
    """The one checkout whose folder or GitHub repository name the task
    mentions, if exactly one.

    Several checkouts can share a repository name: on 2026-09-29 "Umni" named
    /app/data/development/dog-trainer and three worktrees of it
    (dog-trainer-brief, -checkins, -integration), so every first launch was
    refused. A folder the task names outright wins, then the one main checkout
    among them.
    """
    text = (task or "").casefold()

    def _mentions(name: str) -> bool:
        return bool(name) and bool(re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", text))

    named, by_folder = [], []
    for path in checkouts:
        folder = os.path.basename(os.path.normpath(path)).casefold()
        if _mentions(folder):
            by_folder.append(path)
            named.append(path)
        elif _mentions(_origin_repo_name(path).casefold()):
            named.append(path)
    if len(named) == 1:
        return named[0]
    if len(by_folder) == 1:
        return by_folder[0]
    main = [p for p in named if not _is_linked_worktree(p)]
    return main[0] if len(main) == 1 else None


def _worktree_top(path: str, root: str) -> Optional[str]:
    """The worktree folder (the one holding ``.git``) that ``path`` is in, under ``root``."""
    current = os.path.realpath(path)
    root = os.path.realpath(root)
    while _within(current, root) and current != root:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _workspace_from_task_paths(task: str, checkouts: Iterable[str]) -> Optional[str]:
    """The one checkout or managed worktree that the paths in the task point into.

    A task that says "work in /app/data/agent_worktrees/_repos/umni-50d95770/fix-x"
    or "repository /app/data/development/dog-trainer" names its workspace; it
    was refused anyway, because only folder and repository names were read.
    """
    from src.tool_execution import vet_workspace

    try:
        from src.agent_worktree.ownership import managed_worktree_root

        managed = managed_worktree_root()
    except Exception:
        managed = None
    found: Set[str] = set()
    for match in _LOCAL_PATH_RE.finditer(task or ""):
        path = match.group(0).rstrip(".,;:)]}'\"`")
        if not os.path.isabs(path):
            continue
        inside = [c for c in checkouts if _within(path, c)]
        if inside:
            # The innermost checkout: a worktree nested in a root beats the root.
            found.add(max(inside, key=lambda c: len(os.path.realpath(c))))
            continue
        if managed is not None and _within(path, str(managed)):
            top = _worktree_top(path, str(managed))
            if top and vet_workspace(top):
                found.add(top)
    return next(iter(found)) if len(found) == 1 else None


def _shell_sandbox_problem(workspace: str) -> Optional[str]:
    try:
        from src.shell_sandbox import workspace_problem

        return workspace_problem(workspace)
    except Exception:
        return None


def _within(path: str, root: str) -> bool:
    path, root = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(root))
    try:
        return path == root or os.path.commonpath([path, root]) == root
    except ValueError:
        return False


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
        # A chat bound to a broad folder (on 2026-09-29, /app) runs bash on its
        # private-vault grant; a worker without that grant can run it only in
        # the workspace sandbox, which refuses a folder holding the app's data.
        # The Lead Engineer worker lost bash that way. When the task names a
        # checkout inside the chat's folder, bind the worker to it instead: a
        # narrower workspace, and one the sandbox accepts.
        too_broad = _shell_sandbox_problem(pf.workspace)
        if too_broad:
            inside = [c for c in known_checkouts() if vet_workspace(c) and _within(c, pf.workspace)]
            chosen = _checkout_named_in(task, inside) or (
                inside[0] if len(inside) == 1 and pf.needs_workspace else None)
            if chosen:
                pf.workspace = vet_workspace(chosen)
                pf.workspace_source = f"named in the task, inside the parent chat's {inherited_workspace}"
            elif {"bash", "python"} - unavailable:
                pf.warnings.append(
                    f"bash/python can run in {pf.workspace} only with the private-vault grant ({too_broad}); "
                    "pass workspace as the repository to work on so they run sandboxed there")
    elif pf.needs_workspace:
        checkouts = [c for c in known_checkouts() if vet_workspace(c)]
        by_path = _workspace_from_task_paths(task, checkouts)
        chosen = by_path or _checkout_named_in(task, checkouts) or (checkouts[0] if len(checkouts) == 1 else None)
        if chosen:
            pf.workspace = vet_workspace(chosen)
            pf.workspace_source = ("path in the task" if by_path else
                                   "named in the task" if len(checkouts) > 1 else "only checkout")
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
