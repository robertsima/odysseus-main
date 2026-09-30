"""What a chat is waiting on a person for.

A turn that stops short names its needs as `Needs user: <what>` lines (the
agent rules and the self-unblock check ask for them). The agent loop records
the user needs a turn ended with on the chat, and clears them when a later
turn ends without any; the Control Room reads them to list the chat under
"Needs you". Before this, a worker that stopped on "Needs user: approve the
publish request" showed a green Finished pill, and the need was visible only
inside its collapsed hand-back (2026-09-30 harness sweep).

`Needs parent:` lines are not recorded: the chat that started the worker
receives them in the hand-back and can act on them without a person.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

KEY = "open_needs"
MAX_NEEDS = 6
MAX_CHARS = 300


def parse(text: Optional[str]) -> List[str]:
    """The `Needs user:` requests in ``text``, in order, without duplicates."""
    from src.agent_loop import NEEDS_LINE_RE

    needs: List[str] = []
    for match in NEEDS_LINE_RE.finditer(str(text or "")):
        if match.group(1).lower() != "user":
            continue
        what = match.group(2).strip()[:MAX_CHARS]
        if what and what not in needs:
            needs.append(what)
    return needs[:MAX_NEEDS]


def record(session_id: Optional[str], final_text: Optional[str]) -> List[str]:
    """Store the needs the turn ended with, or clear stale ones. Never raises."""
    if not session_id:
        return []
    needs = parse(final_text)
    try:
        from core.database import get_session_settings, update_session_settings

        had = bool((get_session_settings(session_id) or {}).get(KEY))
        if needs:
            update_session_settings(session_id, {KEY: {"needs": needs, "at": time.time()}})
        elif had:
            update_session_settings(session_id, {KEY: None})
    except Exception:
        logger.debug("open needs: could not record for %s", session_id, exc_info=True)
    return needs


def clear(session_id: Optional[str]) -> bool:
    """Drop the chat's recorded needs. True when there was something to drop.

    Called when a new turn STARTS in the chat: whatever it was waiting on has
    been answered (or overtaken), and a turn that still needs a person records
    that again when it ends. Clearing only at the end left a worker that
    stopped, errored or was cancelled mid-turn showing "Needs your input" for
    good after the person had answered (2026-09-30).
    """
    if not session_id:
        return False
    try:
        from core.database import get_session_settings, update_session_settings

        if not (get_session_settings(session_id) or {}).get(KEY):
            return False
        update_session_settings(session_id, {KEY: None})
        return True
    except Exception:
        logger.debug("open needs: could not clear for %s", session_id, exc_info=True)
        return False


def read(settings: Optional[Dict[str, Any]], since: Optional[float] = None) -> List[str]:
    """The needs stored in a chat's settings (empty when there are none).

    ``since`` is when the chat's latest turn began: needs recorded before it
    were overtaken by that turn (answered, or the turn died before it could
    clear them), so they are not open.
    """
    record_ = (settings or {}).get(KEY)
    if not isinstance(record_, dict):
        return []
    if since and (record_.get("at") or 0) and float(record_["at"]) < float(since):
        return []
    return [str(item) for item in (record_.get("needs") or []) if str(item).strip()][:MAX_NEEDS]
