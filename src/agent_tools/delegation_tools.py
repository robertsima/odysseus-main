"""Provider-neutral coding-agent delegation tool."""

from __future__ import annotations

import json


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
        result.setdefault("exit_code", 1 if result.get("error") else 0)
        return result
