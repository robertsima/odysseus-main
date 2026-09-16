"""Provider-neutral coding-agent delegation tool."""

from __future__ import annotations

import json


_FAILED_STATUSES = frozenset({"failed", "error", "cancelled", "canceled", "interrupted"})
_ACTIVE_STATUSES = frozenset({"queued", "running", "started"})
_COMPLETED_STATUSES = frozenset({"completed", "complete", "succeeded", "success", "done"})


def _is_failure(result: dict) -> bool:
    """Whether a provider result actually represents a failed delegation."""
    if result.get("error"):
        return True
    if result.get("is_error") is True or result.get("isError") is True:
        return True
    if str(result.get("status") or "").strip().lower() in _FAILED_STATUSES:
        return True
    code = result.get("exit_code")
    if code is None:
        return False
    try:
        return int(code) != 0
    except (TypeError, ValueError):
        return str(code).strip().lower() not in {"", "0", "success", "ok"}


class DelegationTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = json.loads(content) if (content or "").strip() else {}
        except (TypeError, json.JSONDecodeError):
            return {"error": "delegate_to_agent: JSON object required", "exit_code": 1}
        if not isinstance(args, dict):
            return {"error": "delegate_to_agent: JSON object required", "exit_code": 1}

        from src import delegation

        provider, reason = delegation.selection()
        if provider is None:
            return {
                "error": f"delegate_to_agent: {reason}",
                "providers": [
                    {"id": provider_id, "available": ok, "detail": detail}
                    for provider_id, ok, detail in delegation.availability()
                ],
                "exit_code": 1,
            }
        try:
            result = await provider.delegate(args, ctx or {})
        except Exception as exc:
            return {
                "error": f"delegate_to_agent: {provider.title or provider.id} failed: {exc}",
                "provider": provider.id,
                "exit_code": 1,
            }
        if not isinstance(result, dict):
            return {
                "error": f"delegate_to_agent: {provider.id} returned an invalid result",
                "provider": provider.id,
                "exit_code": 1,
            }
        result.setdefault("provider", provider.id)
        result.setdefault("selection", reason)
        # A provider-neutral ``start`` is only truthfully "started" when it
        # returned a trackable active job. Some remote MCPs acknowledge a
        # request with arbitrary success output; calling that a launched worker
        # led the parent to promise audits it could neither poll nor account
        # for. Preserve the acknowledgement (a retry could duplicate work),
        # but make its uncertainty explicit.
        action = str(args.get("action") or "run").strip().lower()
        status = str(result.get("status") or "").strip().lower()
        task_id = str(result.get("task_id") or "").strip()
        failed = _is_failure(result)
        if failed:
            # Preserve a provider's useful non-zero code (for example a CLI
            # timeout), but repair the contradictory ``status=failed,
            # exit_code=0`` shape into an actual failure for the agent loop.
            try:
                has_nonzero_code = int(result.get("exit_code")) != 0
            except (TypeError, ValueError):
                has_nonzero_code = False
            if not has_nonzero_code:
                result["exit_code"] = 1
        else:
            result["exit_code"] = 0
        if failed:
            result["delegation_state"] = "failed"
            result["delegation_note"] = (
                "The provider reported failure; no worker is confirmed started or completed. "
                "Use the error/result details to diagnose before retrying."
            )
        elif status in _ACTIVE_STATUSES:
            if task_id:
                result["delegation_state"] = "started"
                result["delegation_note"] = (
                    f"Active task {task_id} is {status}; poll that task instead of starting it again."
                )
            else:
                result["delegation_state"] = "unconfirmed"
                result["delegation_note"] = (
                    "The provider reported active work but returned no task id to track it. "
                    "Do not claim it started or retry solely for confirmation."
                )
        elif status in _COMPLETED_STATUSES or action == "run":
            # Contractually `run` waits for a result. A status value, when a
            # provider supplies one, takes precedence over that contract.
            result["delegation_state"] = "completed"
            result["delegation_note"] = "The provider returned a completed delegation result."
        elif action == "start":
            result["delegation_state"] = "unconfirmed"
            # Override vague/provider-specific acknowledgements such as
            # "started three agents": without a task id and active state that
            # assertion is not evidence the caller can use.
            result["delegation_note"] = (
                "The provider accepted the start request but did not return a trackable active task. "
                "Do not claim it started or retry it solely for confirmation."
            )
        return result
