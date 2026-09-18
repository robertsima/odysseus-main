"""Explicit per-chat privilege for reading private vault content.

Private vault material is denied unless the chat owner explicitly enables the
``private_vault_access`` setting for that chat.  This module is deliberately
small and dependency-light so every retrieval/file-tool boundary can use the
same fail-closed decision without classifying an endpoint by URL.
"""

from __future__ import annotations

from typing import Any, Optional

PRIVATE_VAULT_ACCESS_KEY = "private_vault_access"

# These MCP wrappers bypass the dedicated file tools' path/sensitivity
# resolvers. Keep inventory and execution on the same conservative policy.
_PRIVATE_READ_MCP_TOOL_NAMES = frozenset({
    "bash", "python", "read_file", "write_file", "edit_file", "apply_patch",
    "grep", "glob", "ls", "get_workspace", "search_documents", "manage_rag",
    # Standard MCP filesystem server surface, including its deprecated alias.
    # These handlers are external to our per-path sensitivity resolver.
    "read_text_file", "read_media_file", "read_multiple_files", "list_directory",
    "list_directory_with_sizes", "directory_tree", "search_files", "get_file_info",
    "list_allowed_directories", "create_directory", "move_file",
})


def tool_requires_private_grant(tool: str) -> bool:
    """Whether this tool bypasses Odysseus's private-file read boundary."""
    if tool in {"bash", "python"}:
        return True
    if not isinstance(tool, str) or not tool.startswith("mcp__"):
        return False
    parts = tool.split("__", 2)
    return len(parts) == 3 and parts[2].casefold() in _PRIVATE_READ_MCP_TOOL_NAMES


def effective_private_grant(request_granted: bool, settings: Optional[dict] = None) -> bool:
    """Fresh session policy can revoke, never widen, the request's grant."""
    return request_granted is True and isinstance(settings, dict) and settings.get(PRIVATE_VAULT_ACCESS_KEY) is True


def private_tool_denial(tool: str) -> dict:
    """Precise, actionable refusal; this is not a missing workspace/path."""
    return {
        "error": (
            f"Tool '{tool}' was not executed: it can bypass private-file protections, "
            "and this chat has no effective private vault access grant. "
            "This does not mean the workspace or repository path is missing. "
            "Dedicated workspace/file tools can still operate on permitted public files. "
            "Only the user can enable 'Allow private vault reads' in this chat's settings; "
            "that grants private-vault access, not merely repository access."
        ),
        "blocked": True,
        "blocked_reason": "private_vault_grant_required",
        "required_setting": PRIVATE_VAULT_ACCESS_KEY,
        "retryable": False,
        "exit_code": 1,
    }


def allows_private_vault(session_id: Optional[str]) -> bool:
    """Return whether *session_id* has an explicit private-vault grant."""
    if not session_id:
        return False
    try:
        from core.database import get_session_settings

        settings = get_session_settings(session_id) or {}
        return settings.get(PRIVATE_VAULT_ACCESS_KEY) is True
    except Exception:
        # A settings/database failure must never widen a read boundary.
        return False


def context_allows_private(ctx: Any) -> bool:
    """Read the already-resolved privilege from a tool execution context."""
    return isinstance(ctx, dict) and ctx.get("allow_private") is True
