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


# Slice size for an ordered read of a stored output. Deliberately smaller than
# the offload threshold: a recall that pastes back as much as was removed has
# achieved nothing.
_RECALL_SLICE_CHARS = 3000
_RECALL_CHUNK_CHARS = 1400
# Ceiling on an explicitly requested slice. The invariant above held for the
# default and not for the maximum, which was 12,000 — above the 8,000 that the
# most generous context profile keeps inline. A model asking for the biggest
# slice it was allowed therefore got one guaranteed to be too big to keep, and
# every such recall was re-offloaded. Kept at/below the widest inline budget so
# the ceiling honours the same rule the default does.
_RECALL_MAX_SLICE_CHARS = 8000


class RecallToolOutputTool:
    """Read back a tool result that was too large to keep in the conversation.

    The agent loop offloads an oversized result to `tool_output_store` and
    leaves an excerpt naming its ref. This is the way back in: ask a question
    of one stored output (semantic, with a keyword fallback when the vector
    index is down), or read it in order from a character offset.
    """

    async def execute(self, content: str, ctx: dict) -> Dict[str, Any]:
        from src import tool_output_store as store

        args = _parse_args(content)
        # A bare string argument is far more likely to be the ref than a query.
        raw = str(args.get("query") or "").strip()
        ref = str(args.get("ref") or args.get("id") or "").strip()
        if not ref and store.is_ref(raw):
            ref, raw = raw, ""
        query = raw
        session_id = ctx.get("session_id")

        if ref and not store.is_ref(ref):
            return {
                "error": (
                    f"recall_tool_output: {ref!r} is not a stored-output reference. "
                    "Use the `toolout-...` id from the excerpt, or omit `ref` to list "
                    "what is stored for this chat."
                ),
                "exit_code": 1,
            }

        if not ref and not query:
            records = await asyncio.to_thread(store.list_recent, session_id, 10)
            if not records:
                return {"results": "No large tool outputs have been stored in this chat."}
            lines = [
                f"- `{r['ref']}` — {r.get('tool') or 'tool'}"
                f" ({r.get('chars', 0):,} chars, {r.get('lines', 0):,} lines)"
                + (f": {r.get('command')}" if r.get("command") else "")
                for r in records
            ]
            return {"results": "Stored tool outputs (newest first):\n" + "\n".join(lines)}

        if ref and not query:
            return await self._read_slice(store, ref, args)

        results = await asyncio.to_thread(
            store.search, query, ref=ref or None, session_id=session_id, k=_clamp_k(args.get("k"))
        )
        if not results:
            hint = f" in `{ref}`" if ref else " in this chat's stored outputs"
            return {
                "results": (
                    f'Nothing matching "{query}" was found{hint}. '
                    "Try different wording, or read the output in order with "
                    "{\"ref\": \"<ref>\", \"offset\": 0}."
                )
            }

        blocks = []
        for row in results:
            text = (row.get("text") or "").strip()
            if len(text) > _RECALL_CHUNK_CHARS:
                text = text[:_RECALL_CHUNK_CHARS] + "\n... (chunk truncated)"
            blocks.append(
                f"--- `{row.get('ref', '')}` chunk {row.get('chunk_index', '?')}"
                f" ({row.get('match', 'semantic')} match {row.get('score', 0):.2f}) ---\n{text}"
            )
        return {"results": "\n\n".join(blocks)}

    async def _read_slice(self, store, ref: str, args: Dict[str, Any]) -> Dict[str, Any]:
        text = await asyncio.to_thread(store.load, ref)
        if text is None:
            return {
                "error": (
                    f"recall_tool_output: `{ref}` is no longer stored (stored outputs "
                    "are kept for a few days). Re-run the tool if you still need it."
                ),
                "exit_code": 1,
            }
        try:
            offset = max(0, int(args.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = int(args.get("limit") or _RECALL_SLICE_CHARS)
        except (TypeError, ValueError):
            limit = _RECALL_SLICE_CHARS
        limit = max(200, min(limit, _RECALL_MAX_SLICE_CHARS))

        slice_text = text[offset:offset + limit]
        if not slice_text:
            return {
                "results": (
                    f"`{ref}` has {len(text):,} characters; offset {offset:,} is past the end."
                )
            }
        end = offset + len(slice_text)
        header = f"`{ref}` characters {offset:,}-{end:,} of {len(text):,}"
        footer = ""
        if end < len(text):
            footer = (
                f"\n\n[{len(text) - end:,} characters remain — continue with "
                f'{{"ref": "{ref}", "offset": {end}}}]'
            )
        return {"results": f"{header}\n\n{slice_text}{footer}"}
