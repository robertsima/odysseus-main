"""Delegation through a locally installed Claude Code CLI.

This is a thin adapter, deliberately. :mod:`src.agent_tools.claude_code_tools`
is the working implementation — repository approval, the concurrency gate, the
per-repo lock, transcript streaming, the background task runner — and it is
already settings-driven (``claude_code_binary``,
``claude_code_repository_roots``). Re-implementing any of that behind the
provider interface would fork the behaviour and only one fork would keep
getting fixed. So this module answers two questions the old code never asked
("is this usable at all?", "which provider am I?") and forwards everything else.

The CLI is here because it authenticates against a Claude *subscription*
instead of billing per token. That is why "no binary" is reported as a missing
provider rather than quietly falling back to an API-key client.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from typing import Any, Mapping, Optional, Tuple

from src.delegation.base import DelegationProvider, DelegationResult

logger = logging.getLogger(__name__)


class ClaudeCliProvider(DelegationProvider):
    """Runs delegated work in a real checkout via the ``claude`` CLI."""

    id = "claude_code_cli"
    title = "Claude Code CLI"

    def is_available(self) -> Tuple[bool, str]:
        """Cheap probe: is there a binary we could actually execute?

        Only the filesystem is consulted. Version and sign-in checks shell out
        (see ``claude_code_tools.status_report``) and this runs once per model
        round, so those stay where the model can ask for them explicitly with
        ``action="status"``.
        """
        try:
            from src.agent_tools.claude_code_tools import binary_path

            configured = binary_path()
        except Exception as exc:
            # A broken settings read must not look like a working provider.
            logger.debug("claude_code_cli: could not resolve binary path", exc_info=True)
            return False, f"could not resolve the configured binary ({type(exc).__name__}: {exc})"

        path = str(configured)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return True, path
        # A host that installed the CLI the ordinary way has it on PATH under a
        # path no setting predicted; honour that before declaring it missing.
        on_path = shutil.which("claude")
        if on_path:
            return True, on_path
        return False, (
            f"no Claude Code binary at {path} and none named 'claude' on PATH — "
            "install it and set the path in Settings › Agents. A subscription "
            "sign-in is enough; no API key is required."
        )

    async def delegate(
        self,
        request: Mapping[str, Any],
        ctx: Optional[Mapping[str, Any]] = None,
    ) -> DelegationResult:
        """Forward the tool's own argument object to the existing implementation.

        ``ClaudeCodeTool.execute`` takes a JSON string because that is what the
        agent loop hands a tool; re-serialising here keeps this adapter on the
        supported entry point instead of reaching past it into the private
        helpers, which is what would rot.
        """
        ok, detail = self.is_available()
        if not ok:
            return self.unavailable_result(detail)
        from src.agent_tools.claude_code_tools import ClaudeCodeTool

        payload = json.dumps(dict(request or {}))
        result = await ClaudeCodeTool().execute(payload, dict(ctx or {}))
        if isinstance(result, dict):
            # Which provider served the call — the only thing a caller could not
            # already infer, and the thing a multi-provider bug report needs.
            result.setdefault("provider", self.id)
        return result
