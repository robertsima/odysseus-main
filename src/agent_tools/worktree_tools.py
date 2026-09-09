"""Agent-facing tools: the persistent worktree, and the app's own logs.

``manage_agent_worktree`` is the only way the agent can reach the publishing
flow, and it is deliberately narrow. It cannot approve its own work: the
``publish`` action requires an approval code that only exists after a human ran
the operator CLI on the host. Everything else it exposes (start, commit, diff,
status) is local and side-effect-free outside the worktree directory.

``read_app_logs`` is read-only and returns redacted lines — see src/agent_logs.py.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from src.tool_utils import _parse_tool_args

logger = logging.getLogger(__name__)

_ACTIONS = ("status", "start", "commit", "diff", "request_publish",
            "publish", "list_requests", "show_request", "remove")


def _err(message: str, **extra: Any) -> Dict[str, Any]:
    return {"error": message, "exit_code": 1, **extra}


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

        from src.agent_worktree import approval as approval_mod
        from src.agent_worktree import service
        from src.agent_worktree.config import load_config

        cfg = load_config()
        branch = str(args.get("branch") or args.get("name") or "").strip()

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
            return _err(f"manage_agent_worktree {action}: {exc}")

        return _err("manage_agent_worktree: unreachable action")


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
            if action != "tail":
                return _err(f"read_app_logs: unknown action {action!r} (use 'list' or 'tail')")
            result = agent_logs.read_log(
                args.get("name"),
                lines=args.get("lines", agent_logs.DEFAULT_LINES),
                contains=args.get("contains"),
                level=args.get("level"),
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
