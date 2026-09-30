"""A chat's whole transcript, for the agent to look back through.

What the model is sent is a window of the chat: compaction replaces older
messages with a summary (it used to delete them; they now move to the
``chat_message_archive`` table), a long turn trims older messages out of the
request, and earlier turns' tool calls are never replayed, only their final
text. None of that is gone any more. This module reads the chat's archived
and live messages together, in order, with each message's tool calls and
their output, and ``recall_chat_history`` (``RecallChatHistoryTool``) is the
agent's way in: an overview, a search, or a read by position or message id.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Output budget of one call, and of one message inside a list of them.
_READ_CHARS = 20_000
_MESSAGE_CHARS = 8_000
_SNIPPET_CHARS = 240
_MAX_WINDOW = 40


def _metadata(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def transcript(session_id: str) -> List[Dict[str, Any]]:
    """Every message of the chat, archived and live, oldest first.

    Each entry: ``index`` (position in the whole transcript), ``id``,
    ``role``, ``content``, ``metadata``, ``timestamp`` (ISO), ``archived``.
    """
    from core.database import ArchivedChatMessage, ChatMessage, SessionLocal

    db = SessionLocal()
    try:
        rows = []
        for model, archived in ((ArchivedChatMessage, True), (ChatMessage, False)):
            for row in db.query(model).filter(model.session_id == session_id).all():
                rows.append((row.timestamp, archived, row))
    finally:
        db.close()
    # Archived rows sort before live ones at the same instant: compaction
    # only ever archives the older part.
    rows.sort(key=lambda r: (r[0] is None, r[0] or datetime.min, not r[1]))
    out = []
    for index, (ts, archived, row) in enumerate(rows):
        meta = _metadata(row.meta_data)
        meta.pop("_db_id", None)
        out.append({
            "index": index,
            "id": str(row.id),
            "role": row.role,
            "content": row.content or "",
            "metadata": meta,
            "timestamp": ts.isoformat(timespec="seconds") if ts else "",
            "archived": archived,
        })
    return out


def message_text(entry: Dict[str, Any], *, tool_output_chars: Optional[int] = None) -> str:
    """A message as text: its content, then each tool call it made with the output."""
    parts = [str(entry.get("content") or "").strip()]
    for ev in entry.get("metadata", {}).get("tool_events") or []:
        if not isinstance(ev, dict):
            continue
        head = f"[tool {ev.get('tool') or '?'}]"
        if ev.get("command"):
            head += f" {ev['command']}"
        if ev.get("exit_code") not in (None, 0):
            head += f" (exit {ev['exit_code']})"
        if ev.get("stopped"):
            head += " (stopped before it finished)"
        output = str(ev.get("output") or "").strip()
        if tool_output_chars is not None and len(output) > tool_output_chars:
            output = output[:tool_output_chars].rstrip() + " …"
        parts.append(head + (f"\n{output}" if output else ""))
    return "\n\n".join(p for p in parts if p)


def _label(entry: Dict[str, Any]) -> str:
    where = "archived" if entry["archived"] else "in chat"
    stamp = f" {entry['timestamp']}" if entry.get("timestamp") else ""
    return f"#{entry['index']} {entry['role']}{stamp} ({where}, id {entry['id']})"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + f"\n… ({len(text) - limit:,} more chars)"


def overview(session_id: str) -> str:
    entries = transcript(session_id)
    if not entries:
        return "This chat has no stored messages."
    archived = [e for e in entries if e["archived"]]
    lines = [
        f"This chat has {len(entries)} messages, #0 to #{len(entries) - 1}: "
        f"{len(archived)} archived (moved out of the chat by compaction or trimming), "
        f"{len(entries) - len(archived)} still in the chat."
    ]
    if archived:
        lines.append(f"Archived: #{archived[0]['index']} to #{archived[-1]['index']}"
                     f" ({archived[0]['timestamp']} to {archived[-1]['timestamp']}).")
    lines.append("Earliest messages:")
    for e in entries[:6]:
        preview = " ".join(message_text(e, tool_output_chars=0).split())[:140]
        lines.append(f"- {_label(e)}: {preview}")
    lines.append(
        "Search with `query`, read one message with `message` (its #index or id, add `before`/`after` "
        "for its neighbours), or read a range with `start` and `count`."
    )
    return "\n".join(lines)


def _terms(query: str) -> List[str]:
    return [t for t in re.findall(r"[\w./:@-]+", query.lower()) if len(t) >= 2]


def search(session_id: str, query: str, limit: int = 8) -> str:
    terms = _terms(query)
    if not terms:
        return "Give a `query` with at least one word to search for."
    phrase = " ".join(query.lower().split())
    scored = []
    for e in transcript(session_id):
        text = message_text(e)
        low = text.lower()
        hits = [t for t in terms if t in low]
        if not hits:
            continue
        score = len(set(hits)) * 10 + (25 if phrase in low else 0) + min(sum(low.count(t) for t in hits), 20)
        first = min(low.find(t) for t in hits)
        scored.append((score, e, text, first))
    if not scored:
        return f"No message in this chat mentions {query!r}."
    scored.sort(key=lambda s: (-s[0], -s[1]["index"]))
    lines = [f"{len(scored)} message(s) match {query!r}; best {min(limit, len(scored))}:"]
    for _score, e, text, first in scored[:limit]:
        start = max(0, first - _SNIPPET_CHARS // 2)
        snippet = " ".join(text[start:start + _SNIPPET_CHARS].split())
        lines.append(f"- {_label(e)}: …{snippet}…")
    lines.append("Read one in full with `message` (its #index or id).")
    return "\n".join(lines)


def _find(entries: List[Dict[str, Any]], ref: Any) -> Optional[int]:
    text = str(ref or "").strip().lstrip("#")
    if text.isdigit():
        index = int(text)
        return index if 0 <= index < len(entries) else None
    return next((e["index"] for e in entries if e["id"] == text), None)


def read(session_id: str, *, message: Any = None, before: int = 0, after: int = 0,
         start: Optional[int] = None, count: int = 10) -> str:
    entries = transcript(session_id)
    if not entries:
        return "This chat has no stored messages."
    if message is not None and str(message).strip():
        index = _find(entries, message)
        if index is None:
            return (f"No message {message!r} in this chat (it has #0 to #{len(entries) - 1}; "
                    "an id is the one a search or overview printed).")
        lo = max(0, index - max(0, min(int(before or 0), _MAX_WINDOW)))
        hi = min(len(entries), index + 1 + max(0, min(int(after or 0), _MAX_WINDOW)))
    else:
        lo = max(0, min(int(start or 0), len(entries) - 1))
        hi = min(len(entries), lo + max(1, min(int(count or 10), _MAX_WINDOW)))
    window = entries[lo:hi]
    per_message = _READ_CHARS if len(window) == 1 else _MESSAGE_CHARS
    out: List[str] = []
    used = 0
    for e in window:
        block = f"── {_label(e)}\n{_clip(message_text(e), per_message)}"
        if out and used + len(block) > _READ_CHARS:
            out.append(f"(stopped at #{e['index']} to stay under {_READ_CHARS:,} chars; "
                       f"continue with start={e['index']})")
            break
        out.append(block)
        used += len(block)
    else:
        if hi < len(entries):
            out.append(f"(next: start={hi}; the chat has {len(entries)} messages)")
    return "\n\n".join(out)


def _parse_args(content: Any) -> Dict[str, Any]:
    if isinstance(content, dict):
        return content
    text = str(content or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {"query": text}
    return data if isinstance(data, dict) else {"query": text}


class RecallChatHistoryTool:
    """``recall_chat_history``: read back this chat's archived and trimmed messages."""

    async def execute(self, content: Any, ctx: dict) -> Dict[str, Any]:
        args = _parse_args(content)
        session_id = str((ctx or {}).get("session_id") or "").strip()
        if not session_id:
            return {"error": "recall_chat_history: there is no chat to read (no session).", "exit_code": 1}
        query = str(args.get("query") or "").strip()
        message = args.get("message", args.get("message_id", args.get("id")))
        try:
            if message is not None and str(message).strip():
                text = await asyncio.to_thread(
                    read, session_id, message=message,
                    before=int(args.get("before") or 0), after=int(args.get("after") or 0),
                )
            elif query:
                text = await asyncio.to_thread(search, session_id, query, int(args.get("limit") or 8))
            elif args.get("start") is not None:
                text = await asyncio.to_thread(
                    read, session_id, start=int(args.get("start") or 0), count=int(args.get("count") or 10),
                )
            else:
                text = await asyncio.to_thread(overview, session_id)
        except (TypeError, ValueError) as exc:
            return {"error": f"recall_chat_history: bad arguments ({exc}).", "exit_code": 1}
        except Exception as exc:  # noqa: BLE001 - a read tool must not take the turn down
            logger.warning("recall_chat_history failed for %s: %s", session_id, exc)
            return {"error": f"recall_chat_history: could not read the chat ({exc}).", "exit_code": 1}
        return {"results": text}
