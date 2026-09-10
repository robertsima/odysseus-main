"""HTTP surface for Odysseus -> Claude Code delegation.

Lets an authorized caller start a bounded Claude Code CLI task against an
approved Git repository/worktree, poll it, and cancel it — the same
capability the `delegate_to_claude_code` admin agent tool exposes inside
chat, but reachable from outside a chat session (automation, CI, another
admin tool).

Authorization mirrors the Codex cookbook routes (routes/codex_routes.py
`_require_cookbook_scope`): a cookie-session caller must be an admin, since
this surface can edit/commit inside real repositories; an API-token caller is
governed purely by the `claude_code:read` / `claude_code:write` scopes so a
narrowly-scoped token never gets more than it was issued.
"""

from fastapi import APIRouter, Body, HTTPException, Request
from typing import Any

from core.middleware import require_admin
from src.agent_tools.claude_code_tools import get_task_runner, status_report
from src.auth_helpers import require_user

CLAUDE_CODE_READ_SCOPES = {"claude_code:read", "claude_code:write"}
CLAUDE_CODE_WRITE_SCOPES = {"claude_code:write"}


def _require_claude_code_scope(request: Request, allowed: set[str]) -> str:
    """Authorize a Claude Code delegation route.

    API-token callers are governed by scope alone. Cookie-session callers
    must additionally be admin: this surface can edit, commit, and run tests
    inside real repository checkouts, the same trust level as the cookbook
    host-control routes.
    """
    if getattr(request.state, "api_token", False):
        scopes = set(getattr(request.state, "api_token_scopes", []) or [])
        if not scopes.intersection(allowed):
            required = " or ".join(sorted(allowed))
            raise HTTPException(403, f"API token missing required scope: {required}")
        owner = getattr(request.state, "api_token_owner", None)
        if not owner:
            raise HTTPException(403, "API token has no owner")
        return owner
    owner = require_user(request)
    require_admin(request)
    return owner


def setup_claude_code_routes() -> APIRouter:
    router = APIRouter(prefix="/api/claude-code", tags=["claude-code"])

    @router.get("/status")
    async def get_status(request: Request):
        """Preflight for the integration: binary, sign-in state (no token is
        read), approved repositories, callback configuration, live task
        count. Drives the Settings > Tools > Claude Code card and the chat
        tool's action=status."""
        _require_claude_code_scope(request, CLAUDE_CODE_READ_SCOPES)
        return await status_report()

    @router.get("/tasks")
    async def list_tasks(request: Request, limit: int = 50):
        """Bounded task rows (no output blobs). API tokens see their own
        tasks; an admin cookie session sees every task."""
        owner = _require_claude_code_scope(request, CLAUDE_CODE_READ_SCOPES)
        runner = get_task_runner()
        task_owner = owner if getattr(request.state, "api_token", False) else None
        return {"tasks": runner.summaries(owner=task_owner, limit=max(1, min(int(limit or 50), 200)))}

    @router.post("/tasks", status_code=202)
    async def create_task(request: Request, body: dict[str, Any] = Body(default_factory=dict)):
        owner = _require_claude_code_scope(request, CLAUDE_CODE_WRITE_SCOPES)
        runner = get_task_runner()
        result = await runner.start(body, owner=owner)
        if result.get("exit_code") == 1 and "error" in result:
            raise HTTPException(400, result["error"])
        return result

    @router.get("/tasks/{task_id}")
    async def get_task(request: Request, task_id: str):
        owner = _require_claude_code_scope(request, CLAUDE_CODE_READ_SCOPES)
        runner = get_task_runner()
        # API tokens are owner-scoped. Admin cookie sessions may inspect all
        # jobs, matching the app's other admin diagnostics.
        task_owner = owner if getattr(request.state, "api_token", False) else None
        record = runner.get(task_id, owner=task_owner)
        if record is None:
            raise HTTPException(404, "Task not found")
        return record

    @router.post("/tasks/{task_id}/cancel")
    async def cancel_task(request: Request, task_id: str):
        owner = _require_claude_code_scope(request, CLAUDE_CODE_WRITE_SCOPES)
        runner = get_task_runner()
        task_owner = owner if getattr(request.state, "api_token", False) else None
        record = await runner.cancel(task_id, owner=task_owner)
        if record is None:
            raise HTTPException(404, "Task not found")
        return record

    return router
