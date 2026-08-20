"""
rag_tools.py — semantic retrieval over the user's indexed personal documents.

Without this tool the agent's only routes into the vault are `read_file`,
`grep` and `bash`, all of which pull whole documents into the transcript. Vault
notes run to 40k+ characters; once one is in context it stays there and is
re-read on every later turn, so a few reads can push a single agent turn past
60k tokens. This goes through the same ChromaDB index the chat path already
uses (see `chat_processor`), returning the handful of relevant chunks plus the
paths they came from, so the agent can cite a source and only escalate to
`read_file` when it genuinely needs more of one document.
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_K = 5
_MAX_K = 12
# Retrieval returns chunks, not files. Keep the rendered block well under what
# a whole-document read would have cost; `render_retrieved_documents` shares
# this budget across sources so one long note cannot crowd out the others.
_CHAR_BUDGET = 6000


def _resolve_allow_private(session_id: Optional[str]) -> bool:
    """Whether chunks marked private may be returned for this session.

    Same rule as the chat path: retrieved text is pasted into the outbound
    prompt, so private notes may only travel to a local or LAN endpoint. This
    fails closed — an unknown session yields public-only results rather than
    leaking a private note to a hosted API.
    """
    if not session_id:
        return False
    try:
        from src.database import SessionLocal, Session as DbSession
        from src.model_context import is_local_endpoint

        db = SessionLocal()
        try:
            row = db.query(DbSession).filter(DbSession.id == session_id).first()
            if row is None:
                return False
            return is_local_endpoint(row.endpoint_url or "")
        finally:
            db.close()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "search_documents: could not classify endpoint scope (%s); "
            "restricting to public documents",
            exc,
        )
        return False


def _parse_args(content: str) -> Dict[str, Any]:
    """Accept either a JSON object or a bare query string."""
    raw = (content or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {"query": raw}


def _clamp_k(value: Any) -> int:
    try:
        k = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_K
    return max(1, min(k, _MAX_K))


def _source_lines(results: List[Dict[str, Any]]) -> List[str]:
    """One line per distinct source document, best match first.

    The agent needs the real path to be able to escalate to a targeted
    `read_file`, and `describe_chunk` renders a display label rather than a
    path, so emit both.
    """
    from src.vault_markdown import describe_chunk

    seen: Dict[str, Dict[str, Any]] = {}
    for r in results:
        meta = r.get("metadata") or {}
        path = str(meta.get("source") or meta.get("filename") or "").strip()
        if not path or path in seen:
            continue
        seen[path] = r

    lines = []
    for path, r in seen.items():
        meta = r.get("metadata") or {}
        label = describe_chunk(meta)
        lines.append(f"- `{path}` — {label} (similarity {r.get('similarity', 0):.2f})")
    return lines


class SearchDocumentsTool:
    async def execute(self, content: str, ctx: dict) -> Dict[str, Any]:
        args = _parse_args(content)
        query = str(args.get("query") or "").strip()
        if not query:
            return {"error": "search_documents: query is required", "exit_code": 1}

        k = _clamp_k(args.get("k") or args.get("limit"))
        owner = ctx.get("owner")
        allow_private = _resolve_allow_private(ctx.get("session_id"))

        try:
            from src.rag_singleton import get_rag_manager
        except Exception as exc:  # pragma: no cover - defensive
            return {"error": f"search_documents: RAG unavailable ({exc})", "exit_code": 1}

        rag = get_rag_manager()
        if rag is None:
            return {
                "error": (
                    "search_documents: the document index is not available "
                    "(ChromaDB is not reachable). Personal documents cannot be "
                    "searched right now."
                ),
                "exit_code": 1,
            }

        try:
            # chromadb's client is synchronous; keep it off the event loop.
            results = await asyncio.to_thread(
                rag.search, query, k, owner, allow_private
            )
        except Exception as exc:
            logger.warning("search_documents: search failed: %s", exc)
            return {"error": f"search_documents: {exc}", "exit_code": 1}

        if not results:
            return {
                "results": (
                    f'No indexed documents matched "{query}".\n'
                    "The vault may not be indexed yet, or the wording may not appear in it. "
                    "Try different terms before falling back to reading files directly."
                )
            }

        try:
            from src.chat_processor import (
                render_retrieved_documents,
                _env_rag_threshold,
            )

            threshold = _env_rag_threshold()
        except Exception:  # pragma: no cover - defensive
            render_retrieved_documents = None  # type: ignore[assignment]
            threshold = 0.35

        relevant = [r for r in results if r.get("similarity", 0) >= threshold]
        # Retrieval ordering is meaningful even when everything scores low; an
        # empty block would just send the agent back to reading whole files.
        if not relevant:
            relevant = results[:3]

        if render_retrieved_documents is not None:
            body = render_retrieved_documents(relevant, budget=_CHAR_BUDGET)
        else:  # pragma: no cover - defensive
            body = "\n\n---\n\n".join((r.get("document") or "") for r in relevant)

        sources = _source_lines(relevant)
        parts = [body.rstrip()]
        if sources:
            parts.append(
                "Sources (use `read_file` with offset/limit on one of these only "
                "if the excerpt above is genuinely insufficient):\n" + "\n".join(sources)
            )

        logger.info(
            "search_documents: %d/%d chunks above threshold %.2f for %r",
            len(relevant), len(results), threshold, query[:80],
        )
        return {"results": "\n\n".join(parts)}
