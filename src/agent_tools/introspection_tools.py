"""``inspect_runtime`` — the agent reading its own scheduled work and config.

Read-only. Three actions:

* ``tasks`` — the caller's scheduled tasks with the lane each one lands in.
* ``task`` — one task: the prompt its last runs actually sent, their outcomes,
  their tool traces (email calls included), the policy each ran under, the
  circuit-breaker verdict, and every time the scheduler decided not to run it.
* ``config`` — where an effective configuration value came from: an
  environment pin, ``data/settings.json``, this user's prefs, a legacy
  environment fallback, or the code default.

Two properties matter more than the features.

**Owner scoping.** The owner comes from the tool context, which the tool layer
fills from the authenticated session — never from the model's arguments. There
is no parameter that selects another user's tasks, so the model cannot ask for
one. :mod:`src.runtime_introspection` re-checks the owner on the row.

**The payload is untrusted data.** Run outputs, tool results and email bodies
are third-party text. The result is fenced with
``src.prompt_security.untrusted_context_message`` before it can reach the
model, so the content arrives as data behind a header that tells the model not
to follow instructions inside it (``THREAT_MODEL.md``). This tool deliberately
widens a prompt-injection surface; the fence is what makes that acceptable.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from src.tool_utils import _parse_tool_args

logger = logging.getLogger(__name__)

_ACTIONS = ("tasks", "task", "config")


def _err(message: str, **extra: Any) -> Dict[str, Any]:
    return {"error": message, "exit_code": 1, **extra}


def _fenced(label: str, payload: Any) -> str:
    """The payload as text, inside the untrusted-content guard."""
    from src.runtime_introspection import as_untrusted_message

    return as_untrusted_message(label, payload)["content"]


class InspectRuntimeTool:
    """inspect_runtime — scheduled-task forensics and configuration provenance."""

    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = _parse_tool_args(content)
        except ValueError as exc:
            return _err(f"inspect_runtime: invalid JSON arguments ({exc})")

        action = str(args.get("action") or "tasks").strip().lower()
        if action not in _ACTIONS:
            return _err(
                f"inspect_runtime: unknown action {action!r} "
                f"(use one of {', '.join(_ACTIONS)})"
            )

        # Identity comes from the context the tool layer built from the
        # authenticated session. A model-supplied owner is ignored on purpose:
        # accepting one would make this a cross-tenant reader.
        owner = str(ctx.get("owner") or "")

        from src import runtime_introspection as ri

        try:
            if action == "tasks":
                tasks = ri.list_tasks(owner, limit=int(args.get("limit") or 50))
                return {
                    "exit_code": 0,
                    "tasks": tasks,
                    "output": _fenced("scheduled tasks", tasks),
                }

            if action == "task":
                task_id = str(args.get("task_id") or args.get("id") or "").strip()
                if not task_id:
                    return _err("inspect_runtime: task_id is required for action='task'")
                report = ri.task_report(
                    task_id,
                    owner,
                    runs=int(args.get("runs") or ri.DEFAULT_RUNS),
                    include_traces=bool(args.get("include_traces", True)),
                )
                return {
                    "exit_code": 0,
                    "report": report,
                    "output": _fenced(f"task run history for {task_id}", report),
                }

            keys = args.get("key") or args.get("keys")
            if isinstance(keys, str):
                keys = [k.strip() for k in keys.split(",") if k.strip()]
            report = ri.config_report(
                keys or None,
                owner,
                only_non_default=bool(args.get("only_non_default", not keys)),
            )
            return {
                "exit_code": 0,
                "config": report,
                "output": _fenced("configuration provenance", report),
            }
        except ri.NotFound as exc:
            return _err(f"inspect_runtime: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("inspect_runtime failed: %s", exc, exc_info=True)
            return _err(f"inspect_runtime: {exc}")


__all__ = ["InspectRuntimeTool"]
