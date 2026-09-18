"""Typed repository workflows without granting an agent a host shell."""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

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
    @staticmethod
    def precheck(content: str) -> Optional[dict]:
        """Everything a call must satisfy before it is worth an approval.

        The loop used to ask the user to approve a call first and validate it
        after -- so the user was asked to approve a stash_drop missing its
        revision proof, a push carrying an argument push does not take, and a
        push the publish-flow rule forbids outright. Each failed only once it
        ran. This is the pure, side-effect-free half of `execute`; the loop
        runs it before holding, and `execute` runs the same checks.
        """
        error, _args, _action = GitTool._validate(content)
        return error

    @staticmethod
    def _validate(content: str):
        """Returns (error, args, action); error is None when the call may proceed."""
        try:
            args = normalize_git_arguments(_parse_tool_args(content))
        except ValueError:
            return _err("Git requires JSON arguments.", code="invalid_arguments"), None, None
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
            ), args, action
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
            ), args, action
        repository = args.get("repository")
        if action != "repositories" and (
            not isinstance(repository, str) or not repository.strip()
        ):
            return _err(
                "Use repositories, then supply an absolute checkout path.",
                code="invalid_path",
            ), args, action
        if action in RISKY_ACTIONS:
            required = REQUIRED_REVISIONS[action]
            missing = sorted(
                k for k in required
                if not re.fullmatch(r"[0-9a-fA-F]{40}", str(args.get(k) or ""))
            )
            if missing:
                return _err(
                    "Inspect the repository first and supply every exact expected revision "
                    f"required by this action for the confirmation (missing: {', '.join(missing)}).",
                    code="missing_revision",
                ), args, action
        if action in {"push", "force_push_with_lease", "delete_remote_branch"}:
            try:
                from src.agent_worktree.repository_remote import publish_refusal

                refusal = publish_refusal(repository, action)
            except ImportError:
                refusal = None
            if refusal:
                return _err(
                    refusal + ". Use manage_agent_worktree action='request_publish' instead; "
                    "manage_git cannot push this repository.",
                    code="use_publish_flow",
                ), args, action
        return None, args, action

    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_security import owner_is_admin_or_single_user

        if not owner_is_admin_or_single_user(ctx.get("owner")):
            return _err("Git operations require an admin user.", code="admin_required")
        error, args, action = self._validate(content)
        if error:
            return error
        repository = args.get("repository")
        kwargs = {k: v for k, v in args.items() if k not in {"action", "repository"}}
        # Confirmation is the agent loop's job: manage_git has no capability
        # classification, so it fails high and the loop's exact-approval gate
        # holds it whenever untrusted content has influenced the run. Pushing
        # this repository itself is still refused above (use_publish_flow).
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
