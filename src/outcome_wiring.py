"""How worker runs reach the outcomes they serve.

`src/outcomes.py` stores outcomes and `src/blockers.py` shapes what stops work.
This module is the thin seam `agent_control` and `send_to_session` call: it
finds the outcomes a chat or run is linked to, links new workers to them,
records a worker's report, and sets or clears the blocker a refused or
interrupted worker leaves. Every function swallows its own errors and returns
a small result: a failure here must never stop a hand-back.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# Blockers this module sets and may clear when the work runs again. A worker's
# own `Needs user:` line is cleared by its next report instead.
_CLEARABLE_CODES = frozenset({"WORKER_CAPACITY", "RUN_INTERRUPTED"})


def linked(owner: Optional[str], chats: Iterable[Optional[str]] = (), runs: Iterable[Optional[str]] = ()) -> List[Dict[str, Any]]:
    """The owner's unfinished outcomes linked to any of these chats or runs."""
    from src import outcomes

    found: Dict[str, Dict[str, Any]] = {}
    for kind, refs in (("chat", chats), ("agent_run", runs)):
        for ref in refs:
            for o in outcomes.find_by_link(owner, kind, str(ref or "")):
                if o["state"] not in outcomes.CLOSED_STATES:
                    found.setdefault(o["id"], o)
    return list(found.values())


def link_worker(owner: Optional[str], starter_session: Optional[str], worker_session: Optional[str],
                run_id: Optional[str]) -> int:
    """A worker started from a chat that serves an outcome serves it too: link
    the worker's chat and the run to the same outcomes. Returns how many links
    were added."""
    if not (owner and starter_session):
        return 0
    try:
        from src import outcomes

        count = 0
        for o in linked(owner, chats=[starter_session]):
            for kind, ref in (("chat", worker_session), ("agent_run", run_id)):
                if ref and str(ref) != str(starter_session):
                    res = outcomes.add_link(owner, o["id"], kind, str(ref), actor="system")
                    count += bool(res.get("ok") and res.get("created"))
        return count
    except Exception:
        logger.warning("outcome link for worker %s failed", worker_session, exc_info=True)
        return 0


def block(owner: Optional[str], chats: Iterable[Optional[str]], blocker: Any,
          runs: Iterable[Optional[str]] = ()) -> int:
    """Set `blocker` on the outcomes linked to these chats or runs."""
    if not owner:
        return 0
    try:
        from src import outcomes

        count = 0
        for o in linked(owner, chats=chats, runs=runs):
            if o["state"] == "waiting":
                continue
            count += bool(outcomes.set_blocker(owner, o["id"], blocker, "system").get("ok"))
        return count
    except Exception:
        logger.warning("outcome blocker could not be set", exc_info=True)
        return 0


def clear_blockers(owner: Optional[str], chats: Iterable[Optional[str]], runs: Iterable[Optional[str]] = ()) -> int:
    """The work is running again: drop the blockers this module set (a refused
    launch, no capacity, an interrupted run) from the linked outcomes."""
    if not owner:
        return 0
    try:
        from src import outcomes

        count = 0
        for o in linked(owner, chats=chats, runs=runs):
            blocker = o.get("blocker") or {}
            if blocker.get("code") in _CLEARABLE_CODES or (blocker.get("source") or {}).get("kind") == "preflight":
                count += bool(outcomes.set_blocker(owner, o["id"], None, "system").get("ok"))
        return count
    except Exception:
        logger.warning("outcome blocker could not be cleared", exc_info=True)
        return 0


def record_handback(owner: Optional[str], session_id: str, run_id: str, text: Optional[str],
                    run_status: Optional[str], run_summary: Optional[Dict[str, Any]] = None,
                    parent_session: Optional[str] = None) -> Dict[str, Any]:
    """Apply a finished worker's report to its outcomes (`outcomes.record_worker_handback`)."""
    try:
        from src import agent_activity as activity
        from src import outcomes

        summary = dict((activity.get_run(run_id) or {}).get("summary") or {})
        summary.update(run_summary or {})
        if parent_session:
            summary.setdefault("parent_session", parent_session)
        return outcomes.record_worker_handback(owner, session_id, run_id, text, run_status, summary)
    except Exception:
        logger.warning("outcome hand-back for run %s failed", run_id, exc_info=True)
        return {"outcomes": [], "error": "failed"}


def block_capacity(owner: Optional[str], chat: Optional[str], limit: int, running: int) -> int:
    """A launch was refused at the chat's worker limit: say so on its outcomes."""
    if not (owner and chat):
        return 0
    try:
        from src import blockers

        return block(owner, [chat], blockers.worker_capacity(limit, running, session_id=chat))
    except Exception:
        logger.warning("capacity blocker could not be built", exc_info=True)
        return 0


def block_preflight(owner: Optional[str], chat: Optional[str], preflight: Any) -> int:
    """A launch was refused by worker preflight: say so on the chat's outcomes."""
    if not (owner and chat):
        return 0
    try:
        from src import blockers

        return block(owner, [chat], blockers.from_preflight(preflight.blocked_payload()))
    except Exception:
        logger.warning("preflight blocker could not be built", exc_info=True)
        return 0
