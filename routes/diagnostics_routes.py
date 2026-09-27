"""Diagnostics routes — /api/diagnostics/*, /api/db/stats, /api/rag/stats, /api/test/youtube, /api/test-research."""

import asyncio
import logging
import os
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, HTTPException, Form, Query, Request
from fastapi.responses import Response

from services.youtube.youtube_handler import extract_youtube_id, extract_transcript_async
from core.constants import DEFAULT_HOST, DATA_DIR
from core.middleware import require_admin

logger = logging.getLogger(__name__)

# Opt-in API-token scope for pulling the diagnostics bundle from outside the
# browser (e.g. Claude Code on another machine with an Odysseus token). Never
# part of a token profile, so no existing or newly minted token has it unless
# an admin switches it on.
DIAGNOSTICS_READ_SCOPE = "diagnostics:read"


def _require_diagnostics_reader(request: Request, *, include_messages: bool = False) -> Optional[str]:
    """Authorize a diagnostics-bundle route; return the owner it runs as.

    A browser session must be an admin. An API token needs the
    ``diagnostics:read`` scope and an owner who is still an admin, because the
    bundle spans every user's log lines. A token can never ask for message
    content: that can carry private vault text, and a token's caller is
    usually a hosted model.
    """
    if getattr(request.state, "api_token", False):
        scopes = set(getattr(request.state, "api_token_scopes", []) or [])
        if DIAGNOSTICS_READ_SCOPE not in scopes:
            raise HTTPException(403, f"API token missing required scope: {DIAGNOSTICS_READ_SCOPE}")
        owner = getattr(request.state, "api_token_owner", None)
        if not owner:
            raise HTTPException(403, "API token has no owner")
        auth_mgr = getattr(request.app.state, "auth_manager", None)
        if auth_mgr is not None and getattr(auth_mgr, "is_configured", False) and not auth_mgr.is_admin(owner):
            raise HTTPException(403, "Diagnostics bundle needs a token owned by an admin")
        if include_messages:
            raise HTTPException(403, "include_messages is only available from an admin browser session")
        return owner
    require_admin(request)
    from src.auth_helpers import get_current_user

    return get_current_user(request)


def setup_diagnostics_routes(
    rag_manager,
    rag_available: bool,
    research_handler,
    memory_vector=None,
) -> APIRouter:
    router = APIRouter(tags=["diagnostics"])

    @router.get("/api/diagnostics/services")
    async def get_service_health(request: Request) -> Dict[str, Any]:
        """Consolidated degraded-state report for ChromaDB, SearXNG, email,
        ntfy, and provider endpoints. Non-intrusive probes — safe to poll."""
        require_admin(request)
        from src.service_health import collect_service_health
        return await collect_service_health(rag_manager, memory_vector)

    @router.get("/api/diagnostics/logs")
    async def get_diagnostics_logs(request: Request, limit: int = 200) -> Dict[str, Any]:
        require_admin(request)
        limit = max(1, min(limit, 1000))
        try:
            log_file = os.path.join(DATA_DIR, "logs", "app.log")
            if not os.path.exists(log_file):
                return {"status": "success", "logs": []}

            # Safe tail read of the log file (max 5MB via rotation)
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()

            tail_lines = lines[-limit:] if len(lines) > limit else lines
            tail_lines = [line.rstrip('\r\n') for line in tail_lines]

            return {
                "status": "success",
                "logs": tail_lines
            }
        except Exception as e:
            logger.error(f"Diagnostics logs retrieval error: {e}")
            raise HTTPException(500, f"Failed to retrieve logs: {str(e)}")

    @router.get("/api/diagnostics/bundle")
    async def get_diagnostics_bundle(
        request: Request,
        minutes: float = 60,
        session: Optional[List[str]] = Query(None),
        include_messages: bool = False,
        max_lines: Optional[int] = None,
    ) -> Response:
        """Zip of recent redacted logs plus the configuration of every chat,
        worker, and loadout they mention (src/diagnostics_bundle.py).
        ``session`` may repeat or be comma-separated; those chats are always
        included, with their parent and child workers."""
        owner = _require_diagnostics_reader(request, include_messages=include_messages)
        from src import diagnostics_bundle

        result = await diagnostics_bundle.build_bundle(
            minutes=minutes, session_ids=session, include_messages=include_messages,
            max_lines=max_lines or diagnostics_bundle.DEFAULT_MAX_LINES, owner=owner,
            rag_manager=rag_manager, memory_vector=memory_vector,
        )
        logger.info(
            "[diagnostics] bundle built for %s: %d file(s), %d byte(s), %d session(s), messages=%s, errors=%d",
            owner or "-", result.summary["files"], len(result.data), len(result.summary["sessions"]),
            bool(include_messages), len(result.summary["errors"]),
        )
        return Response(
            content=result.data,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{result.filename}"',
                "Cache-Control": "no-store",
            },
        )

    @router.get("/api/diagnostics/bundle/summary")
    async def get_diagnostics_bundle_summary(
        request: Request,
        minutes: float = 60,
        session: Optional[List[str]] = Query(None),
        max_lines: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Preview of a bundle: log line counts and the chats and loadouts
        it would include. Reads logs and stores only; no probes."""
        _require_diagnostics_reader(request)
        from src import diagnostics_bundle

        return await asyncio.to_thread(
            diagnostics_bundle.summarize, minutes=minutes, session_ids=session,
            max_lines=max_lines or diagnostics_bundle.DEFAULT_MAX_LINES,
        )

    @router.get("/api/diagnostics/route_latency")
    async def get_route_latency(request: Request) -> Dict[str, Any]:
        """Return content-free, normalized per-route latency aggregates."""
        require_admin(request)
        from src.route_latency import stats as route_latency_stats
        return {"routes": route_latency_stats()}

    @router.get("/api/db/stats")
    async def get_database_stats(request: Request) -> Dict[str, Any]:
        require_admin(request)
        try:
            from core.database import get_detailed_stats
            return get_detailed_stats()
        except Exception as e:
            logger.error(f"DB stats error: {e}")
            raise HTTPException(500, "Failed to retrieve database statistics")

    @router.get("/api/rag/stats")
    async def get_rag_stats(request: Request) -> Dict[str, Any]:
        require_admin(request)
        if rag_available and rag_manager:
            return rag_manager.get_stats()
        return {"error": "RAG system not available"}

    @router.get("/api/test/youtube")
    async def test_youtube(request: Request, url: str) -> Dict[str, Any]:
        require_admin(request)
        try:
            video_id = extract_youtube_id(url)
            if not video_id:
                return {"error": "Invalid YouTube URL"}

            data = await extract_transcript_async(url, video_id)
            return {
                "video_id": video_id,
                "transcript_success": data.get("success", False),
                "transcript_length": len(data.get("transcript", "")) if data.get("success") else 0,
                "transcript_preview": (data.get("transcript", "")[:500] + "...")
                    if data.get("success") and len(data.get("transcript", "")) > 500
                    else data.get("transcript", ""),
                "error": data.get("error") if not data.get("success") else None,
            }
        except Exception as e:
            return {"error": str(e)}

    @router.post("/api/test-research")
    async def test_research(request: Request, query: str = Form("What is machine learning?")) -> Dict[str, Any]:
        require_admin(request)
        try:
            endpoint = f"http://{DEFAULT_HOST}:8000/v1/chat/completions"
            model = "gpt-oss-120b"
            result = await research_handler.call_research_service(query, endpoint, model)
            return {
                "status": "success",
                "query": query,
                "result_preview": result[:200] + "..." if len(result) > 200 else result,
                "result_length": len(result),
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "query": query}

    return router
