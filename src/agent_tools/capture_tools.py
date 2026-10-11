"""`capture`: the agent routing one line the person wants remembered.

The owner comes from the context and the actor is `agent:<session_id>`. The tool
shares `src.capture` with `POST /api/capture`, so a retry with the same
`idempotency_key` never writes twice, and a regular user's task becomes a note
reminder instead of reaching the shared Todoist account.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from src.tool_utils import _parse_tool_args

logger = logging.getLogger(__name__)


def _err(message: str, **extra: Any) -> Dict[str, Any]:
    return {"error": message, "exit_code": 1, **extra}


class CaptureTool:
    """capture: route one task, reminder, appointment or follow-up to where it belongs."""

    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = _parse_tool_args(content)
        except ValueError as exc:
            return _err(f"capture: invalid JSON arguments ({exc})")
        text = str(args.get("text") or "").strip()
        if not text:
            return _err("capture: text is required (the one line to capture)")

        from src.owner_identity import effective_storage_owner

        owner = effective_storage_owner((ctx or {}).get("owner"))
        if not owner:
            return _err("capture: no signed-in user for this call")
        session_id = str((ctx or {}).get("session_id") or "")
        actor = f"agent:{session_id}" if session_id else "agent:unknown"
        tz_offset = args.get("tz_offset")
        try:
            from src import capture

            result = await capture.capture(
                owner, text, idempotency_key=args.get("idempotency_key"), destination=args.get("destination") or None,
                due=args.get("due") or None, actor=actor, tz=args.get("tz"),
                tz_offset=int(tz_offset) if tz_offset is not None else None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("capture tool failed: %s", exc, exc_info=True)
            return _err(f"capture: {exc}")
        if result.get("status") == "failed":
            return _err(f"capture: {result.get('error') or 'failed'}", **{k: v for k, v in result.items() if k != "error"})
        return {"exit_code": 0, "output": json.dumps(result, ensure_ascii=False, default=str), **result}
