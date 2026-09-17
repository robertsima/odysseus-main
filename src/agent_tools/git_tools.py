"""Typed repository workflows without granting an agent a host shell."""

from __future__ import annotations

import logging
import os
import re

from src.git_tool_contract import (
    CREATION_ACTIONS,
    HISTORY_ACTIONS,
    LOCAL_ACTIONS,
    REMOTE_ACTIONS,
    REQUIRED_REVISIONS,
    RISKY_ACTIONS,
    normalize_git_arguments,
)
from src.tool_utils import _parse_tool_args

from .worktree_tools import _err, _repository_read_token

logger = logging.getLogger(__name__)


def _write_token(ctx: dict) -> str | None:
    """Write integration opt-in, not merely possession of a read credential."""
    from core.database import get_session_settings

    if not ctx.get("session_id"):
        return None
    if os.environ.get("ODYSSEUS_GITHUB_MCP_WRITE", "").lower() not in {
        "1",
        "true",
        "yes",
    }:
        return None
    settings = get_session_settings(ctx["session_id"], strict=True) or {}
    allowed = settings.get("allowed_mcp_servers")
    if allowed is not None and (
        not isinstance(allowed, list) or not {"*", "github_write"}.intersection(allowed)
    ):
        return None
    # The shared credential is usable only at the fixed public GitHub host.
    from src.github_credentials import github_token_from_env

    return github_token_from_env(public_only=True)


class GitTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_security import owner_is_admin_or_single_user

        if not owner_is_admin_or_single_user(ctx.get("owner")):
            return _err("Git operations require an admin user.", code="admin_required")
        try:
            args = normalize_git_arguments(_parse_tool_args(content))
        except ValueError:
            return _err("Git requires JSON arguments.", code="invalid_arguments")
        action = str(args.get("action") or "repositories").strip().lower()
        allowed = LOCAL_ACTIONS.get(
            action,
            CREATION_ACTIONS.get(
                action, HISTORY_ACTIONS.get(action, REMOTE_ACTIONS.get(action))
            ),
        )
        if action != "repositories" and allowed is None:
            return _err(
                "Unsupported Git action. Use the advertised typed actions; arbitrary commands "
                "and conflict-resolving merges are not exposed.",
                code="unsupported_action",
            )
        permitted = (
            {"action"}
            if action == "repositories"
            else {"action", "repository"} | allowed
        )
        if set(args) - permitted:
            return _err(
                f"Unsupported arguments for Git action {action!r}. "
                f"Send only: {', '.join(sorted(permitted))}; omit unused fields. "
                "Non-empty overrides and unknown arguments are not accepted.",
                code="invalid_arguments",
            )
        repository = args.get("repository")
        if action != "repositories" and (
            not isinstance(repository, str) or not repository.strip()
        ):
            return _err(
                "Use repositories, then supply an absolute checkout path.",
                code="invalid_path",
            )
        kwargs = {k: v for k, v in args.items() if k not in {"action", "repository"}}
        if action in RISKY_ACTIONS:
            required = REQUIRED_REVISIONS[action]
            if any(
                not re.fullmatch(r"[0-9a-fA-F]{40}", str(args.get(k) or ""))
                for k in required
            ):
                return _err(
                    "Inspect the repository first and supply every exact expected revision "
                    "required by this action for the confirmation.",
                    code="missing_revision",
                )
            from src.tool_approvals import consume_once_grant

            if not consume_once_grant(
                ctx.get("session_id"), "manage_git", content.strip()
            ):
                return _err(
                    "This Git operation needs a fresh confirmation for these exact arguments. "
                    "Ask the user through the tool approval card; an agent cannot approve itself.",
                    code="approval_required",
                    blocked=True,
                )
        else:
            # ask_all can also require confirmation for a routine mutation.
            # The loop peeks; the handler consumes so one grant runs one call.
            from src.tool_approvals import consume_once_grant

            consume_once_grant(ctx.get("session_id"), "manage_git", content.strip())
        try:
            from src.agent_worktree import repository_sync as sync
        except ImportError:
            return _err(
                "Git support dependency is missing; rebuild the app image.",
                code="dependency_missing",
            )
        try:
            if action == "repositories":
                return {"exit_code": 0, "repositories": await sync.list_repositories()}
            if action in CREATION_ACTIONS:
                from src.agent_worktree.repository_creation import execute_creation

                token = _repository_read_token(ctx) if action == "clone" else None
                result = await execute_creation(
                    action, repository, token=token, **kwargs
                )
            elif action in HISTORY_ACTIONS:
                from src.agent_worktree.repository_history import execute_history

                result = await execute_history(action, repository, **kwargs)
            elif action in LOCAL_ACTIONS:
                from src.agent_worktree.repository_local import execute_local

                result = await execute_local(action, repository, **kwargs)
            else:
                from src.agent_worktree.repository_remote import execute_remote

                token = (
                    _write_token(ctx)
                    if action
                    in {"push", "force_push_with_lease", "delete_remote_branch"}
                    else _repository_read_token(ctx)
                )
                if (
                    action in {"push", "force_push_with_lease", "delete_remote_branch"}
                    and not token
                ):
                    return _err(
                        "GitHub write integration is disabled, disallowed for this agent, or "
                        "missing its token. Enable it explicitly before pushing.",
                        code="write_integration_unavailable",
                    )
                result = await execute_remote(action, repository, token=token, **kwargs)
            return {"exit_code": 0, "result": result}
        except sync.RepositorySyncError as exc:
            return _err(str(exc), code=exc.code)
        except Exception as exc:
            logger.exception(
                "Unexpected manage_git failure action=%s error_type=%s",
                action,
                type(exc).__name__,
            )
            return _err(
                "Git operation failed. Check repository access/configuration; no shell fallback "
                "was attempted. Inspect status before retrying a mutation.",
                code="git_operation_failed",
            )
