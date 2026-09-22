"""Agent-facing tools: the persistent worktree, and the app's own logs.

``manage_agent_worktree`` is the only way the agent can reach the publishing
flow, and it is deliberately narrow. It cannot approve its own work: the
``publish`` action requires an approval code that only exists after a human ran
the operator CLI on the host. Everything else it exposes (start, commit, diff,
status) is local and side-effect-free outside the worktree directory.

``read_app_logs`` is read-only and returns redacted lines — see src/agent_logs.py.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from src.tool_utils import _parse_tool_args

logger = logging.getLogger(__name__)

_ACTIONS = ("status", "start", "commit", "diff", "request_publish",
            "publish", "list_requests", "show_request", "remove",
            "repo_list", "repo_status", "repo_pull")


def _err(message: str, **extra: Any) -> Dict[str, Any]:
    return {"error": message, "exit_code": 1, **extra}


# Actions that work on one named worktree, in the order an agent needs them.
_BRANCH_ACTIONS = ("commit", "diff", "request_publish", "remove")


def _next_step(code: str, action: str, branch: str) -> Dict[str, Any]:
    """The call that fixes a failure the agent caused, so it doesn't spend
    rounds guessing the publish sequence (start → edit → commit →
    request_publish)."""
    if code == "MISSING_BRANCH":
        return {"code": code, "required_fields": ["name"],
                "next_action": {"action": "status"},
                "hint": (f"'{action}' needs the worktree's name. Call status to list the "
                         "agent worktrees, or start one with {\"action\": \"start\", \"name\": \"...\"}.")}
    if code == "WORKTREE_NOT_STARTED":
        return {"code": code, "required_fields": ["name"],
                "next_action": {"action": "start", "name": branch},
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
    from src.github_credentials import github_token_from_env

    return github_token_from_env(public_only=True)


class AgentWorktreeTool:
    """manage_agent_worktree — isolated worktree plus human-gated publishing."""

    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = _parse_tool_args(content)
        except ValueError as exc:
            return _err(f"manage_agent_worktree: invalid JSON arguments ({exc})")

        action = str(args.get("action") or "status").strip().lower()
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

        if action in _BRANCH_ACTIONS and not branch:
            return _err(f"manage_agent_worktree {action}: 'name' is required",
                        **_next_step("MISSING_BRANCH", action, branch))

        try:
            if action == "status":
                return {"exit_code": 0, "status": await service.status(branch or None, cfg=cfg)}

            if action == "start":
                if not branch:
                    return _err("manage_agent_worktree: 'name' is required to start a worktree")
                return {"exit_code": 0, "worktree": await service.ensure_worktree(branch, cfg=cfg)}

            if action == "commit":
                return {
                    "exit_code": 0,
                    "result": await service.commit(
                        branch, str(args.get("message") or ""), cfg=cfg
                    ),
                }

            if action == "diff":
                return {"exit_code": 0, "diff": await service.diff_summary(branch, cfg=cfg)}

            if action == "request_publish":
                view = await service.request_publish(
                    branch,
                    title=str(args.get("title") or ""),
                    body=str(args.get("body") or ""),
                    requested_by=str(ctx.get("owner") or "") or None,
                    cfg=cfg,
                )
                return {
                    "exit_code": 0,
                    "request": view,
                    "note": (
                        "Nothing has been pushed. Show the operator the file list "
                        "and the approval command, then wait for them to hand back "
                        "an approval code."
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
                return {"exit_code": 0, "requests": approval_mod.list_requests(cfg=cfg)}

            if action == "show_request":
                request_id = str(args.get("request_id") or "").strip()
                if not request_id:
                    return _err("manage_agent_worktree: 'request_id' is required")
                return {"exit_code": 0, "request": approval_mod.get_request(request_id, cfg=cfg)}

            if action == "remove":
                return {"exit_code": 0, "result": await service.remove_worktree(branch, cfg=cfg)}
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as text
            # Messages from this package are already credential-scrubbed.
            logger.warning("manage_agent_worktree %s failed: %s", action, exc)
            code = getattr(exc, "code", None)
            return _err(f"manage_agent_worktree {action}: {exc}",
                        **(_next_step(code, action, branch) if code else {}))

        return _err("manage_agent_worktree: unreachable action")

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
            if action != "tail":
                return _err(f"read_app_logs: unknown action {action!r} (use 'list', 'tail' or 'trace')")
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
