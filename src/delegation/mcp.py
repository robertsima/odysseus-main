"""Delegation through an administrator-selected MCP tool.

MCP is the transport here, not an authentication shortcut.  The remote server
owns its authentication (including OAuth) and exposes a coding-agent tool;
Odysseus only maps the provider-neutral delegation request onto that tool.
This keeps vendor credentials out of Odysseus and works with any server whose
tool accepts the standard request fields.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional, Tuple

from src.delegation.base import DelegationProvider, DelegationResult


def _configured_tool() -> str:
    from src.settings import get_setting

    return str(get_setting("delegation_mcp_tool", "") or "").strip()


def _disabled_tools() -> Dict[str, set[str]]:
    """Read the same per-server deny-list used by the normal MCP dispatcher."""
    from core.database import McpServer, SessionLocal

    disabled: Dict[str, set[str]] = {}
    db = SessionLocal()
    try:
        for server in db.query(McpServer).all():
            try:
                names = json.loads(server.disabled_tools or "[]")
            except (TypeError, json.JSONDecodeError):
                names = []
            if isinstance(names, list):
                disabled[str(server.id)] = {str(name) for name in names}
    finally:
        db.close()
    return disabled


def _split_qualified(name: str) -> Tuple[str, str]:
    parts = str(name or "").split("__", 2)
    if len(parts) != 3 or parts[0] != "mcp" or not parts[1] or not parts[2]:
        return "", ""
    return parts[1], parts[2]


class McpDelegationProvider(DelegationProvider):
    id = "mcp"
    title = "MCP coding agent"

    def is_available(self) -> Tuple[bool, str]:
        qualified = _configured_tool()
        server_id, tool_name = _split_qualified(qualified)
        if not server_id:
            return False, (
                "choose a connected coding-agent tool in delegation_mcp_tool "
                "(for example mcp__server__delegate)"
            )
        from src.tool_utils import get_mcp_manager

        manager = get_mcp_manager()
        if manager is None:
            return False, "the MCP manager is not running"
        matches = {
            str(item.get("qualified_name")): item
            for item in manager.get_all_tools(_disabled_tools())
        }
        item = matches.get(qualified)
        if item is None:
            return False, f"configured tool {qualified!r} is not exposed by a connected MCP server"
        if item.get("is_disabled"):
            return False, f"configured tool {qualified!r} is disabled in MCP settings"
        return True, f"connected to {qualified}"

    async def delegate(
        self,
        request: Mapping[str, Any],
        ctx: Optional[Mapping[str, Any]] = None,
    ) -> DelegationResult:
        del ctx  # Authentication and identity are owned by the configured server.
        ok, detail = self.is_available()
        if not ok:
            return self.unavailable_result(detail)

        from src.settings import get_setting
        from src.tool_utils import get_mcp_manager

        defaults = get_setting("delegation_mcp_default_arguments", {}) or {}
        if not isinstance(defaults, dict):
            return {
                "error": "MCP coding agent: delegation_mcp_default_arguments must be an object",
                "provider": self.id,
                "exit_code": 1,
            }
        args = dict(defaults)
        args.update(dict(request or {}))
        # Provider selection belongs to Odysseus, not to the remote tool.
        args.pop("provider", None)
        manager = get_mcp_manager()
        result = await manager.call_tool(_configured_tool(), args)
        if not isinstance(result, dict):
            result = {"output": result}
        result.setdefault("exit_code", 1 if result.get("error") else 0)
        result.setdefault("provider", self.id)
        return result


# Optional-provider modules self-register when the registry imports them.
from src.delegation import register  # noqa: E402  (intentional registration cycle)

register(McpDelegationProvider())
