"""GET /api/settings/shell-sandbox: whether the workspace shell sandbox works.

The settings page shows the ``shell_sandbox`` choice, but whether the sandbox
can actually run is decided by the container (bubblewrap, user namespaces, a
mountable /proc), and until 2026-09-28 the only place that said so was one
startup log line. This reports the cached probe (``shell_sandbox.status``)
next to the two settings so the UI can show it beside the control.
"""

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Depends

from core.middleware import require_admin


def setup_shell_sandbox_routes() -> APIRouter:
    router = APIRouter(prefix="/api/settings", tags=["settings"])

    @router.get("/shell-sandbox")
    async def shell_sandbox_status(_admin: None = Depends(require_admin)) -> Dict[str, Any]:
        """``{"mode", "network", "available", "degraded", "reason"}``.

        ``status()`` probes once and caches the answer (a failure is
        re-probed after ten minutes); the first call can run bwrap, so it
        runs off the event loop.
        """
        from src import shell_sandbox

        state = await asyncio.to_thread(shell_sandbox.status)
        return {
            "mode": shell_sandbox.mode(),
            "network": shell_sandbox.network_enabled(),
            "available": bool(state.get("available")),
            "degraded": bool(state.get("degraded")),
            "reason": str(state.get("reason") or "") or None,
        }

    return router
