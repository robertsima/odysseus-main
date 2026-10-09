"""Agent-facing tools: the persistent worktree, and the app's own logs.

``manage_agent_worktree`` is the only way the agent can reach the publishing
flow, and it is deliberately narrow. It cannot approve its own work: the
``publish`` action requires an approval code that only exists after a human ran
the operator CLI on the host. Everything else it exposes (start, sync, commit,
diff, status) is local and side-effect-free outside the worktree directory, apart
from sync's fetch of the base. The one push without a code, ``publish_sync``,
only carries a clean merge of the base tip onto a head a person already
approved and published (service.publish_sync).

``read_app_logs`` is read-only and returns redacted lines — see src/agent_logs.py.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict

from src.tool_utils import _parse_tool_args

logger = logging.getLogger(__name__)

_ACTIONS = ("status", "diagnose", "start", "sync", "commit", "diff", "request_publish",
            "publish", "publish_sync", "list_requests", "show_request", "checks", "cleanup",
            "repo_list", "repo_status", "repo_pull")

# Refused by policy rather than merely unknown: `remove` runs
# `git worktree remove --force` and then prunes, which discards uncommitted
# work. `cleanup` is the loss-free alternative: it refuses a dirty worktree
# and deletes the branch only while another ref still holds its commits. Only
# `cleanup discard_uncommitted=true` discards, and only on the person's own
# words in a top-level chat; it saves a recovery snapshot ref first.
_FORBIDDEN_ACTIONS = ("remove",)


# 2026-10-02: the person wrote "clean up any other worktrees or branches ...
# Even if they have existing uncommitted data." Cleanup refused three dirty
# ones and the admin agent told them it had not discarded the work. The
# person's own words now unlock a recoverable discard. A worker, a hand-back
# or a harness note never does.
_DISCARD_OK_RE = re.compile(
    r"\b(?:discard\w*|throw(?:ing)?\s+(?:(?:it|them|that|those)\s+)?away)\b[^.\n]{0,60}"
    r"\b(?:uncommitted|unsaved|untracked|changes|data|work)\b"
    r"|\b(?:uncommitted|unsaved|untracked)\b[^.\n]{0,60}"
    r"\b(?:discard\w*|throw\w*|delet\w+|wipe\w*|drop\w*|remov\w+|los[et]|lose)\b"
    r"|\beven\s+(?:if|though|when|with)\b[^.\n]{0,60}\b(?:uncommitted|unsaved|untracked)\b"
    r"|\beven\s+if\s+(?:they|it|those)\s+(?:has|have|had|contain\w*)\b[^.\n]{0,40}"
    r"\b(?:changes|data|work)\b",
    re.IGNORECASE,
)
_DISCARD_DENIED_RE = re.compile(
    r"\b(?:don'?t|do\s+not|never|without|not)\s+(?:\w+\s+){0,2}"
    r"(?:discard\w*|throw\w*|delet\w+|wipe\w*|los[ei]\w*)\b",
    re.IGNORECASE,
)
_HUMAN_SOURCES = frozenset({"", "user", "steer"})


def _discard_authorised(ctx: dict) -> tuple[bool, str]:
    """Whether this chat's person allowed discarding uncommitted work.

    Needs a top-level chat (a worker has no person of its own) whose latest
    real message, skipping harness notes, hand-backs and context envelopes,
    says so. Returns ``(allowed, reason_when_not)``.
    """
    session_id = str(ctx.get("session_id") or "")
    if not session_id:
        return False, "this call is not tied to a chat with a person"
    try:
        from src.agent_tools.loadout_tools import _chat_history

        is_worker, history = _chat_history(session_id, str(ctx.get("owner") or "") or None)
    except Exception:  # noqa: BLE001 - unverifiable means not allowed
        logger.debug("discard authorisation lookup failed", exc_info=True)
        return False, "the chat could not be read to check"
    if is_worker:
        return False, "workers cannot discard work; only a top-level chat the person writes in can"
    for message in reversed(history or []):
        get = message.get if isinstance(message, dict) else (lambda k, d=None, m=message: getattr(m, k, d))
        if get("role") != "user":
            continue
        meta = get("metadata") or {}
        text = str(get("content") or "")
        if (meta.get("trusted") is False or meta.get("kind") == "peer"
                or str(meta.get("source") or "") not in _HUMAN_SOURCES
                or text.lstrip().startswith("[Harness note")):
            continue
        if _DISCARD_OK_RE.search(text) and not _DISCARD_DENIED_RE.search(text):
            return True, ""
        return False, "the person's latest message does not say to discard uncommitted work"
    return False, "no message from the person was found in this chat"


def _err(message: str, **extra: Any) -> Dict[str, Any]:
    return {"error": message, "exit_code": 1, **extra}


def _setup_plan(worktree: Any) -> Dict[str, Any]:
    """What the worktree needs before its tests run (src/toolchains.setup_plan)."""
    path = worktree.get("path") if isinstance(worktree, dict) else None
    if not path:
        return {}
    try:
        from src.toolchains import setup_plan

        plan = dict(setup_plan(str(path)))
    except Exception:  # noqa: BLE001 - a hint must never fail the start
        logger.debug("worktree setup plan failed for %s", path, exc_info=True)
        return {}
    if plan.get("install_first"):
        plan["note"] = ("Run install_first in your shell before any test: the worktree is a fresh "
                        "checkout without dependencies. Package caches persist per repository, so "
                        "repeat installs are quick.")
    return plan


def _request_visible(request: Dict[str, Any], ctx: dict) -> bool:
    """Whether this chat's agent may read ``request``: it is the owner's and was
    made from this chat or one of its workers."""
    owner = str(ctx.get("owner") or "")
    if owner and request.get("requested_by") != owner:
        return False
    session_id = str(ctx.get("session_id") or "")
    if session_id:
        from src.agent_worktree import approval as approval_mod

        return session_id in approval_mod.session_lineage(str(request.get("session_id") or ""))
    return True


def _similar_request_hint(request_id: str, cfg, ctx: Dict[str, Any] | None = None) -> str:
    """Open publish requests an unknown id was probably meant to be.

    Ids are 32 hex characters that agents copy between chats; on 2026-09-29 a
    parent retyped 5920c8ebbb87… as 5920c8eb87… for its worker, which then
    reported "no approval request" and stopped. Only a hint: the lookup stays
    exact, and publishing still needs the human's code.
    """
    from src.agent_worktree import approval as approval_mod

    wanted = request_id.strip().lower()
    try:
        ctx = ctx or {}
        open_requests = [r for r in approval_mod.list_requests(
                             cfg=cfg,
                             owner=str(ctx.get("owner") or "") or None,
                             session_id=str(ctx.get("session_id") or "") or None)
                         if r.get("status") in ("pending", "granted")]
    except Exception:  # noqa: BLE001 - a hint must never mask the real error
        return ""
    close = [r for r in open_requests
             if wanted and (str(r.get("id", "")).startswith(wanted[:6]) or str(r.get("id", "")).endswith(wanted[-6:]))]
    shown = close or open_requests[:3]
    if not shown:
        return ""
    return (". Open requests: "
            + "; ".join(f"{r.get('id')} ({r.get('branch')}, {r.get('status')})" for r in shown)
            + ". Copy the id exactly.")


# Actions that work on one named worktree, in the order an agent needs them.
def _sync_summary(result: Dict[str, Any], abort: bool) -> str:
    """One plain line on top of a sync result, for the chat's tool card."""
    result = result or {}
    if abort:
        return f"Dropped the unfinished merge on {result.get('branch') or 'the branch'}."
    outcome, base = result.get("result"), result.get("base") or "the base"
    if outcome == "already_up_to_date":
        return f"Already up to date with {base}."
    if outcome == "merged":
        return f"Merged {base} into {result.get('branch')} cleanly."
    if outcome == "conflicted":
        paths = result.get("conflicted_paths") or []
        shown = ", ".join(str(p) for p in paths[:5]) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")
        return f"Merging {base} conflicts in {len(paths)} file(s): {shown}. The merge is open for resolving."
    return f"Sync finished: {outcome}."


_BRANCH_ACTIONS = ("diagnose", "sync", "commit", "diff", "request_publish", "publish_sync",
                   "checks", "cleanup")


def _next_step(code: str, action: str, branch: str, repository: str = "",
               can_discard: bool = False) -> Dict[str, Any]:
    """The call that fixes a failure the agent caused, so it doesn't spend
    rounds guessing the publish sequence (start → edit → commit →
    request_publish)."""
    where = {"repository": repository} if repository else {}
    if code in ("HEAD_MISMATCH", "WORKTREE_METADATA_INVALID"):
        return {"code": code, "next_action": {"action": "diagnose", "branch": branch, **where},
                "hint": "Use the registered worktree. Diagnose before changing settings or making a clone."}
    if code in ("BRANCH_IS_BASE", "BASE_MISMATCH", "BASE_NOT_FOUND", "BASE_REQUIRED", "INVALID_BASE"):
        return {"code": code, "required_fields": ["name", "base"],
                "next_action": {"action": "start", "name": "<task-name>", "base": "origin/main", **where},
                "hint": ("'name' is the new task/branch name; 'base' is the existing ref or commit "
                         "it starts from (origin/main, a branch, or a SHA), and expected_base "
                         "(full SHA) makes start refuse if the base moved. Fetch first if the base "
                         "is missing (manage_git fetch on the repository).")}
    if code in ("INVALID_REPOSITORY", "AMBIGUOUS_WORKTREE", "REPOSITORY_REQUIRED"):
        return {"code": code, "required_fields": ["repository"],
                "next_action": {"action": "repo_list"},
                "hint": ("Pass repository=<absolute checkout path> from repo_list (the project's "
                         "main checkout; a linked worktree resolves to it).")}
    if code == "WORKTREE_DIRTY":
        return {"code": code, "next_action": {"action": "diff", "name": branch, **where},
                "hint": ("Commit the work, or ask the user what to do with it. cleanup never discards "
                         "changes unless the person's own words in this chat allow it"
                         + ("; they do here: retry cleanup with discard_uncommitted=true, which saves "
                            "a recovery snapshot ref first." if can_discard else "."))}
    if code == "MERGE_IN_PROGRESS":
        return {"code": code, "next_action": {"action": "diff", "name": branch, **where},
                "hint": ("Resolve the listed conflicts and call commit, or call sync with "
                         "abort=true to drop the merge.")}
    if code == "CONFLICT_MARKERS":
        return {"code": code, "hint": ("Edit the named files to the intended content (no "
                                       "<<<<<<< ======= >>>>>>> lines left), then commit again.")}
    if code == "SYNC_NEEDS_APPROVAL":
        return {"code": code, "next_action": {"action": "request_publish", "name": branch, **where},
                "hint": ("Only a clean merge of the base's remote tip into an approved, published "
                         "head goes without approval. Ask for approval with request_publish.")}
    if code == "MISSING_BRANCH":
        return {"code": code, "required_fields": ["name"],
                "next_action": {"action": "status"},
                "hint": (f"'{action}' needs the worktree's name. Call status to list the "
                         "agent worktrees, or start one with {\"action\": \"start\", \"name\": \"...\"}.")}
    if code == "WORKTREE_NOT_STARTED":
        return {"code": code, "required_fields": ["name"],
                "next_action": {"action": "start", "name": branch, **where},
                "hint": ("Start the worktree first, make and commit the changes in it, "
                         f"then call {action} again with the same name.")}
    if code == "INVALID_BRANCH":
        return {"code": code, "required_fields": ["name"],
                "next_action": {"action": "status"},
                "hint": ("Use a short task name such as 'cache-fix' (or the full "
                         "agent/odysseus/<name> branch). Call status to list existing ones.")}
    return {"code": code}


def _repository_read_token(ctx: dict) -> str | None:
    """Borrow GitHub's read credential only within the live integration ceiling.

    The token never comes from model arguments, a repository config, or the
    separate human-gated publishing credential. Sessionless calls are anonymous.
    """
    session_id = ctx.get("session_id")
    if not session_id:
        return None
    from core.database import get_session_settings

    settings = get_session_settings(session_id, strict=True) or {}
    allowed = settings.get("allowed_mcp_servers")
    if allowed is not None and (
        not isinstance(allowed, list) or not {"*", "github_read"}.intersection(allowed)
    ):
        return None
    # The token GitHub MCP uses, for the host it uses (github.com or
    # GITHUB_HOST). Which remote actually receives it is decided per URL by
    # repository_sync._transport: only that same host, never another.
    from src.github_credentials import github_token_from_env

    return github_token_from_env()


def _text_arg(args: dict, key: str) -> str:
    value = args.get(key)
    return value.strip() if isinstance(value, str) else ""


def _worktree_path_as_name(cfg, branch: str, repository: str) -> tuple[str, str]:
    """Read ``repository=<a managed worktree's own path>`` as that worktree.

    A worker standing in its worktree passes the path it works in; on
    2026-09-29 `diff` and `status` were refused for exactly that ("'name' is
    required", "only physical checkouts ..."). A worktree under
    ``_repos/<key>/<leaf>`` is named by its leaf and belongs to the main
    repository its verified `.git` pointer names. Anything else is returned
    unchanged, so every other path keeps its existing checks. The source
    repository's direct-root layout uses the same verified routing.
    """
    import os

    from src.agent_worktree.config import REPOSITORY_WORKTREE_DIR
    from src.agent_worktree.validation import is_inside

    if not repository:
        return branch, repository
    path = os.path.realpath(repository)
    group = os.path.dirname(path)
    if (
        not is_inside(path, cfg.worktree_root)
        or not (group == os.path.realpath(cfg.worktree_root)
                or os.path.basename(os.path.dirname(group)) == REPOSITORY_WORKTREE_DIR)
        or not os.path.isfile(os.path.join(path, ".git"))
    ):
        return branch, repository
    try:
        from src.agent_worktree.repository_sync import linked_worktree_main

        main = str(linked_worktree_main(path))
    except Exception:  # noqa: BLE001 - unverifiable: let the normal checks refuse it
        return branch, repository
    leaf = os.path.basename(path).replace("__", "/")
    return (branch or leaf), main


def _default_repository_refusal(cfg) -> Dict[str, Any] | None:
    """Refuse a repository-less start when the run is bound to another project.

    Without ``repository`` a worktree is made of the configured source
    repository. On 2026-09-28 a worker working on the Umni checkout called
    start with only a name and a base and got a worktree of Odysseus instead.
    When this run's workspace sits in a different checkout, say so and create
    nothing; passing ``repository`` (either one) is the explicit choice.
    """
    try:
        from src.agent_worktree.repository_sync import workspace_repository
        from src.agent_worktree.validation import is_inside

        workspace = workspace_repository()
    except Exception:  # noqa: BLE001 - no workspace information: keep the default
        return None
    if workspace is None:
        return None
    here = str(workspace)
    if is_inside(here, cfg.source_repo) or is_inside(here, cfg.worktree_root):
        return None
    return _err(
        "manage_agent_worktree start: no 'repository' was given, which means the configured "
        f"source repository {cfg.source_repo}, but this run works in {here}. Nothing was "
        f"created. Pass repository={here!r} (plus base, e.g. 'origin/main') for a worktree of "
        f"that project, or repository={cfg.source_repo!r} if you really mean the source repository.",
        code="REPOSITORY_REQUIRED",
        next_action={"action": "start", "repository": here, "name": "<task-name>",
                     "base": "origin/main"},
    )


class AgentWorktreeTool:
    """manage_agent_worktree — isolated worktree plus human-gated publishing."""

    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = _parse_tool_args(content)
        except ValueError as exc:
            return _err(f"manage_agent_worktree: invalid JSON arguments ({exc})")

        action = str(args.get("action") or "status").strip().lower()
        # "list" is what a model reaches for to see existing worktrees
        # (2026-09-28: refused as an unknown action mid-run); status lists them.
        action = {"list": "status", "ls": "status"}.get(action, action)
        if action in _FORBIDDEN_ACTIONS:
            return _err(
                f"manage_agent_worktree: action {action!r} is not permitted by policy: "
                "force-removing a worktree discards its uncommitted work. Use 'cleanup', "
                "which removes only a clean worktree and keeps any branch whose commits "
                "exist nowhere else, or ask the user to remove it.",
                code="forbidden_by_policy",
            )
        if action not in _ACTIONS:
            return _err(
                f"manage_agent_worktree: unknown action {action!r}. "
                f"Valid actions: {', '.join(_ACTIONS)}"
            )

        if action.startswith("repo_"):
            return await self._repo_execute(dict(args, action=action), ctx)

        from src.agent_worktree import approval as approval_mod
        from src.agent_worktree import service
        from src.agent_worktree.config import load_config

        cfg = load_config()
        branch = str(args.get("branch") or args.get("name") or "").strip()
        repository = _text_arg(args, "repository")
        if action != "start":
            branch, repository = _worktree_path_as_name(cfg, branch, repository)

        if action in _BRANCH_ACTIONS and not branch:
            return _err(f"manage_agent_worktree {action}: 'name' is required",
                        **_next_step("MISSING_BRANCH", action, branch, repository))

        try:
            if action == "status":
                return {"exit_code": 0, "status": await service.status(
                    branch or None, cfg=cfg, repository=repository or None,
                    owner=str(ctx.get("owner") or "") or None,
                    session_id=str(ctx.get("session_id") or "") or None)}

            if action == "diagnose":
                from src.agent_worktree.repository_sync import workspace_repository

                workspace = workspace_repository()
                return {"exit_code": 0, "diagnosis": await service.diagnose(
                    branch, cfg=cfg, repository=repository or None,
                    expected_head=args.get("expected_head"),
                    workspace=str(workspace) if workspace else None)}

            if action == "start":
                name = _text_arg(args, "name")
                requested = _text_arg(args, "branch")
                if not (name or requested):
                    return _err("manage_agent_worktree: 'name' is required to start a worktree",
                                **_next_step("MISSING_BRANCH", action, "", repository))
                if not repository:
                    refusal = _default_repository_refusal(cfg)
                    if refusal:
                        return refusal
                worktree = await service.ensure_worktree(
                    name,
                    branch=requested or None,
                    cfg=cfg,
                    repository=repository or None,
                    base=_text_arg(args, "base") or None,
                    expected_base=_text_arg(args, "expected_base") or None,
                )
                out: Dict[str, Any] = {"exit_code": 0, "worktree": worktree}
                # A fresh worktree has no node_modules: its tests failed with
                # "jest: not found" until someone thought to run `npm ci`.
                setup = _setup_plan(worktree)
                if setup:
                    out["setup"] = setup
                return out

            if action == "commit":
                return {
                    "exit_code": 0,
                    "result": await service.commit(
                        branch, str(args.get("message") or ""), cfg=cfg,
                        repository=repository or None,
                    ),
                }

            if action == "sync":
                # 2026-10-08: agents had no way to bring the base into a managed
                # worktree; "behind / conflicts" episodes ended with a person on GitHub.
                abort = args.get("abort") in (True, "true", "True", 1)
                result = await service.sync(
                    branch, cfg=cfg, repository=repository or None,
                    expected_head=None if abort else args.get("expected_head"), abort=abort,
                    token=None if abort else _repository_read_token(ctx))
                return {"exit_code": 0, "output": _sync_summary(result, abort), "sync": result}

            if action == "publish_sync":
                result = await service.publish_sync(
                    branch, owner=str(ctx.get("owner") or "") or None, cfg=cfg,
                    repository=repository or None)
                logger.warning(
                    "manage_agent_worktree: base sync pushed without a new approval session=%s "
                    "branch=%s request=%s head=%s", ctx.get("session_id"), result.get("branch"),
                    result.get("request_id"), result.get("head_sha"))
                return {"exit_code": 0, "output": (
                    f"Pushed {result.get('branch')} @ {str(result.get('head_sha') or '')[:12]}: a clean merge "
                    f"of {result.get('base_branch') or 'the base'} onto the approved branch, so no new "
                    "approval was needed."), "published": result}

            if action == "diff":
                return {"exit_code": 0, "diff": await service.diff_summary(
                    branch, cfg=cfg, repository=repository or None)}

            if action == "checks":
                return await self._checks(args, ctx, cfg, branch, repository)

            if action == "cleanup":
                discard = args.get("discard_uncommitted") in (True, "true", "True", 1)
                if discard:
                    allowed, why = _discard_authorised(ctx)
                    if not allowed:
                        return _err(
                            "manage_agent_worktree cleanup: discard_uncommitted refused: " + why
                            + ". The person has to say so in their own words in this chat (for "
                            "example \"discard the uncommitted work\"). Nothing was removed.",
                            code="DISCARD_NOT_AUTHORISED")
                result = await service.cleanup(
                    branch, cfg=cfg, repository=repository or None, discard_uncommitted=discard)
                snap = result.get("discarded_snapshot")
                if snap:
                    logger.warning(
                        "manage_agent_worktree: person-authorised discard session=%s branch=%s "
                        "paths=%s snapshot=%s", ctx.get("session_id"), result.get("branch"),
                        snap.get("paths"), snap.get("ref"))
                return {"exit_code": 0, "cleanup": result}

            if action == "request_publish":
                view = await service.request_publish(
                    branch,
                    title=str(args.get("title") or ""),
                    body=str(args.get("body") or ""),
                    requested_by=str(ctx.get("owner") or "") or None,
                    cfg=cfg,
                    repository=repository or None,
                    session_id=str(ctx.get("session_id") or "") or None,
                    expected_head=args.get("expected_head"),
                )
                return {
                    "exit_code": 0,
                    "request": view,
                    "note": (
                        "Nothing has been pushed. Tell the user the change is waiting for "
                        "approval: the chat shows a publish request they can review, and "
                        "approving it there pushes the branch and opens the draft PR. Do not "
                        "ask for an approval code and do not call publish yourself."
                    ),
                }

            if action == "publish":
                request_id = str(args.get("request_id") or "").strip()
                code = str(args.get("approval_code") or "").strip()
                if not request_id or not code:
                    return _err(
                        "manage_agent_worktree: publish needs both 'request_id' and "
                        "the 'approval_code' a human produced with the operator CLI"
                    )
                result = await service.publish(request_id, code, cfg=cfg)
                return {"exit_code": 0, "published": result}

            if action == "list_requests":
                # Only this person's requests from this chat and its workers; the
                # unfiltered list let an agent present another chat's request as
                # its own (2026-10-02).
                return {"exit_code": 0, "requests": approval_mod.list_requests(
                    cfg=cfg,
                    owner=str(ctx.get("owner") or "") or None,
                    session_id=str(ctx.get("session_id") or "") or None,
                )}

            if action == "show_request":
                request_id = str(args.get("request_id") or "").strip()
                if not request_id:
                    return _err("manage_agent_worktree: 'request_id' is required")
                request = approval_mod.get_request(request_id, cfg=cfg)
                if not _request_visible(request, ctx):
                    # Same answer as an unknown id, so a guessed id confirms nothing.
                    return _err(f"manage_agent_worktree show_request: no approval request {request_id}")
                return {"exit_code": 0, "request": request}

        except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
            # Messages from this package are already credential-scrubbed.
            logger.warning("manage_agent_worktree %s failed: %s", action, exc)
            code = getattr(exc, "code", None)
            hint = (_similar_request_hint(str(args.get("request_id") or ""), cfg, ctx)
                    if action in ("publish", "show_request") and "no approval request" in str(exc) else "")
            can_discard = (code == "WORKTREE_DIRTY" and _discard_authorised(ctx)[0])
            if code == "HEAD_MISMATCH":
                recovery = _next_step(code, action, branch, repository)
                recovery["next_action"]["expected_head"] = args.get("expected_head")
                return _err(f"manage_agent_worktree {action}: {exc}", **recovery)
            return _err(f"manage_agent_worktree {action}: {exc}{hint}",
                        **(_next_step(code, action, branch, repository, can_discard) if code else {}))

        return _err("manage_agent_worktree: unreachable action")

    async def _checks(self, args: dict, ctx: dict, cfg, branch: str, repository: str) -> Dict[str, Any]:
        """CI checks of the branch's open pull request (src/agent_worktree/checks.py).

        Read-only. The wait lives here, not in the worker's shell: on
        2026-10-02 workers polled CI with `sleep 60` between model rounds.
        """
        from src.agent_worktree import checks as checks_mod
        from src.agent_worktree import service
        from src.agent_worktree.github import pr_access_blockers

        rcfg, resolved, _path = await service._locate(cfg, branch, repository or None)
        blockers = pr_access_blockers(rcfg)
        if blockers:
            return _err("manage_agent_worktree checks: GitHub is not readable from here: "
                        + "; ".join(blockers) + ". Tell the person; CI cannot be checked until it is set up.",
                        code="GITHUB_UNAVAILABLE")
        try:
            return await checks_mod.branch_checks(
                rcfg, resolved, wait_seconds=args.get("wait_seconds") or 0,
                session_id=str(ctx.get("session_id") or "") or None)
        except checks_mod.NoOpenPullRequest:
            where = {"repository": repository} if repository else {}
            return _err(
                f"manage_agent_worktree checks: {resolved} has no open pull request, so there are no "
                "checks to read. Call status and read the branch's publish block: with no request, "
                "commit and call request_publish; with a request waiting, the person has not approved "
                "it yet; with a request published, the PR may be merged or closed.",
                code="NO_OPEN_PULL_REQUEST", next_action={"action": "status", "name": resolved, **where})


    async def _repo_execute(self, args: dict, ctx: dict) -> dict:
        """Separate scoped checkout sync from the app's publishing worktree."""
        from src.git_tool_contract import normalize_worktree_repo_arguments
        from src.tool_security import owner_is_admin_or_single_user

        if not owner_is_admin_or_single_user(ctx.get("owner")):
            return _err("Repository operations require an admin user.", code="admin_required")
        args = normalize_worktree_repo_arguments(args)
        action = args["action"]
        accepted = {"action"} if action == "repo_list" else {"action", "repository"}
        if set(args) - accepted:
            return _err(
                "Repository sync accepts only action and repository; the configured upstream "
                "cannot be overridden with a branch, remote, URL, or command.", code="invalid_arguments",
            )
        if action != "repo_list" and (
            not isinstance(args.get("repository"), str) or not args["repository"].strip()
        ):
            return _err("Use repo_list, then pass its absolute repository path.", code="invalid_path")
        try:
            from src.agent_worktree import repository_sync
        except ImportError:
            return _err("Repository sync dependency is missing; rebuild the app image.", code="dependency_missing")
        try:
            if action == "repo_list":
                return {"exit_code": 0, "repositories": await repository_sync.list_repositories()}
            if action == "repo_status":
                result = await repository_sync.repository_status(args["repository"])
            else:
                result = await repository_sync.pull_repository(
                    args["repository"], token=_repository_read_token(ctx),
                )
            return {"exit_code": 0, "result": result}
        except repository_sync.RepositorySyncError as exc:
            return _err(str(exc), code=exc.code)
        except Exception:
            # Transport/config exceptions can contain auth headers or embedded
            # credentials. Never emit their text or traceback to the model/log.
            logger.warning("Scoped repository operation failed: action=%s", action)
            return _err(
                "Repository operation failed; check checkout access and the GitHub read "
                "integration. No shell fallback was attempted.", code="repository_sync_failed",
            )


class ReadAppLogsTool:
    """read_app_logs — tail Odysseus's own logs, redacted."""

    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = _parse_tool_args(content)
        except ValueError as exc:
            return _err(f"read_app_logs: invalid JSON arguments ({exc})")

        from src import agent_logs

        action = str(args.get("action") or "tail").strip().lower()
        try:
            if action == "list":
                return {"exit_code": 0, "logs": agent_logs.logs_index()}
            if action == "trace":
                found = agent_logs.trace(args.get("id") or args.get("contains") or "",
                                         lines=args.get("lines", agent_logs.DEFAULT_LINES),
                                         owner=ctx.get("owner"))
                body = "\n".join(found["lines"]) or "(no log lines mention this id)"
                runs = "\n".join(json.dumps(r, default=str) for r in found["runs"]) or "(no activity runs)"
                return {"exit_code": 0, "trace": found,
                        "output": (f"Trace {found['id']}: {found['line_count']} log line(s) across "
                                   f"{', '.join(found['log_files']) or 'no log files'}"
                                   + (" (showing the newest)" if found["truncated"] else "")
                                   + f"\n{body}\n\nActivity runs:\n{runs}")}
            if action == "bundle":
                return await self._bundle(args, ctx)
            if action != "tail":
                return _err(f"read_app_logs: unknown action {action!r} (use 'list', 'tail', 'trace' or 'bundle')")
            result = agent_logs.read_log(
                args.get("name"),
                lines=args.get("lines", agent_logs.DEFAULT_LINES),
                contains=args.get("contains"),
                level=args.get("level"),
                since_minutes=args.get("since_minutes"),
            )
        except RuntimeError as exc:
            return _err(f"read_app_logs: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("read_app_logs failed: %s", exc)
            return _err(f"read_app_logs: {exc}")

        header = f"{result['name']} ({result['line_count']} lines, modified {result['modified']})"
        body = "\n".join(result["lines"]) or "(no matching lines)"
        return {
            "exit_code": 0,
            "output": f"{header}\n{body}",
            "log": {k: v for k, v in result.items() if k != "lines"},
        }

    @staticmethod
    async def _bundle(args: Dict[str, Any], ctx: dict) -> Dict[str, Any]:
        """Write a diagnostics bundle (src/diagnostics_bundle.py) under the
        data dir for the admin to download. Never includes message text: the
        agent cannot opt a chat's content into a file meant to be shared."""
        from src import diagnostics_bundle

        ids = [s for s in (args.get("id"), args.get("session_id"), ctx.get("session_id")) if s]
        result = await diagnostics_bundle.build_bundle(
            minutes=args.get("since_minutes") or diagnostics_bundle.DEFAULT_MINUTES,
            session_ids=ids, include_messages=False, owner=ctx.get("owner"),
        )
        path = diagnostics_bundle.write_bundle(result)
        summary = result.summary
        return {
            "exit_code": 0,
            "path": path,
            "summary": summary,
            "output": (f"Diagnostics bundle written to {path} ({len(result.data)} bytes zipped, "
                       f"{summary.get('files', 0)} files, {summary.get('log_lines', 0)} log lines, "
                       f"{len(summary.get('sessions') or [])} chat(s), "
                       f"{len(summary.get('loadouts') or [])} loadout(s), "
                       f"{len(summary.get('errors') or [])} component error(s)). No message text is included. "
                       "The admin can also download one from Settings > System > Diagnostics bundle."),
        }
