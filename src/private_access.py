"""Explicit per-chat privilege for reading private vault content.

Private vault material is denied unless the chat owner explicitly enables the
``private_vault_access`` setting for that chat.  This module is deliberately
small and dependency-light so every retrieval/file-tool boundary can use the
same fail-closed decision without classifying an endpoint by URL.
"""

from __future__ import annotations

from typing import Any, Optional

PRIVATE_VAULT_ACCESS_KEY = "private_vault_access"


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
