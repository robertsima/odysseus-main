"""A chat's task checklist: the steps of the request it is working on.

Claude Code keeps a todo list that it re-reads every turn, so a multi-part
request is worked through to the end. Odysseus had two tools for that
(``update_plan`` and ``todowrite``) and neither was ever read back: the plan
lived in the browser's localStorage, the todos in a file nothing opened. On
2026-09-29 the user sent "delete the stale worktrees, then fix the CI run"; the
first half was dropped, and the user had to ask "check that all the steps I
asked for were completed".

The checklist is now stored on the chat (session settings key
``task_checklist``), shown to the agent beside each request while it has open
items (a protected note, so context trimming keeps it), and an agentic turn
that updated it and is about to end with items still open is asked to carry
on (src.agent_loop). It is markdown: ``- [ ]`` open, ``- [x]`` done.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

KEY = "task_checklist"
MAX_CHARS = 8192
# A checklist nobody touched for this long is from an old request.
MAX_AGE_S = 7 * 24 * 3600

_ITEM_RE = re.compile(r"^\s*[-*+]\s*\[(?P<mark>[ xX~-])\]\s*(?P<text>.+?)\s*$", re.MULTILINE)
_CLEAR_WORDS = frozenset({"", "clear", "none", "done", "reset"})


def items(plan: str) -> List[Dict[str, Any]]:
    """Checklist lines as ``{"text", "done"}``. ``[~]``/``[-]`` count as done
    (dropped or not applicable)."""
    return [{"text": m.group("text"), "done": m.group("mark") != " "}
            for m in _ITEM_RE.finditer(str(plan or ""))]


def open_items(plan: str) -> List[str]:
    return [it["text"] for it in items(plan) if not it["done"]]


def counts(plan: str) -> tuple:
    rows = items(plan)
    return sum(1 for it in rows if it["done"]), len(rows)


def is_clear_request(plan: str) -> bool:
    return str(plan or "").strip().lower() in _CLEAR_WORDS


def from_todos(todos: Iterable[Dict[str, Any]]) -> str:
    """Render ``todowrite`` items as the same markdown checklist."""
    lines = []
    for todo in todos or []:
        if not isinstance(todo, dict):
            continue
        text = str(todo.get("content") or "").strip()
        if not text:
            continue
        status = str(todo.get("status") or "pending")
        mark = "x" if status == "completed" else " "
        lines.append(f"- [{mark}] {text}" + (" (in progress)" if status == "in_progress" else ""))
    return "\n".join(lines)


def load(session_id: Optional[str], settings: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The chat's checklist, or None when it has none (or only an old one)."""
    if settings is None:
        if not session_id:
            return None
        try:
            from core.database import get_session_settings

            settings = get_session_settings(session_id) or {}
        except Exception:
            return None
    record = (settings or {}).get(KEY)
    if not isinstance(record, dict) or not str(record.get("plan") or "").strip():
        return None
    if time.time() - float(record.get("updated_at") or 0) > MAX_AGE_S:
        return None
    return record


def save(session_id: Optional[str], plan: str) -> Optional[Dict[str, Any]]:
    """Store (or, for an empty plan, clear) the chat's checklist."""
    if not session_id:
        return None
    try:
        from core.database import update_session_settings

        if is_clear_request(plan):
            update_session_settings(session_id, {KEY: None})
            return None
        record = {"plan": str(plan).strip()[:MAX_CHARS], "updated_at": time.time()}
        update_session_settings(session_id, {KEY: record})
        return record
    except Exception:
        logger.debug("task checklist: could not save for %s", session_id, exc_info=True)
        return None


def _age(updated_at: float) -> str:
    seconds = max(0, time.time() - float(updated_at or 0))
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{int(seconds // 60)} min ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} days ago"


def turn_note(record: Dict[str, Any]) -> str:
    """The note shown beside the request while the checklist has open items."""
    plan = str(record.get("plan") or "").strip()
    done, total = counts(plan)
    return (
        f"## Task checklist for this chat ({done}/{total} done, updated {_age(record.get('updated_at'))})\n"
        "These are the steps of the request this chat is working on. Carry on with the open "
        "items before you finish, and keep the list current with `update_plan` (the full "
        "checklist, finished steps ticked `- [x]`). If the user's latest message starts "
        "something else, rewrite the checklist for it, or clear it with an empty plan when "
        "there is nothing left to track.\n\n" + plan
    )


def needs_clause(has_parent: bool = False) -> str:
    """The `Needs user:` / `Needs parent:` contract, written once.

    A fragment that continues a sentence ("..., end with one line per need:
    ..."). Four places used to spell it out (the base rules, the self-unblock
    check, the checklist nudge, the worker's parent-chat note) and drifted
    (2026-10-01). The base rules keep their own short `Needs user:` form.

    ``has_parent``: a worker's need may be one the chat that started it can
    grant, so it is offered `Needs parent:` too.
    """
    if has_parent:
        return (
            "end with one line per need: `Needs parent: <what>` for a tool, permission, workspace "
            "or branch the chat that started you can grant, or `Needs user: <what>` for what only "
            "a person can give (an approval, a credential, a real decision)"
        )
    return "end with one line per need: `Needs user: <what>`"


def continue_directive(plan: str, *, has_parent: bool = False) -> str:
    """Asked of a turn about to end while the checklist it updated has open items."""
    remaining = open_items(plan)
    shown = "\n".join(f"- [ ] {text}" for text in remaining[:12])
    return (
        "Your task checklist still has open items:\n" + shown + "\n\n"
        "Carry on with the next one now. If they are already done, tick them with `update_plan`; "
        "if they no longer apply, rewrite or clear the checklist. If an item needs something "
        "only someone else can give, " + needs_clause(has_parent) + "."
    )
