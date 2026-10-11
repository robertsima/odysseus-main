"""Whose consent lets a chat keep working with the agents it already has.

Under delegation policy `explicit` the person must ask for other agents. That
used to be judged on each message, so a later "please just finish this" could
not resume the worker the person had asked for (2026-10-10). Consent now
belongs to the chat:

* `delegation_granted` in the chat's settings is set when a message passes the
  explicit check, and set to False when the person forbids agents.
* A chat that has launched child workers may keep working with them.
* A delegation tool the person switched on in the chat's tool settings is
  recorded in `delegation_tools_enabled` and is open for that tool entirely.

What consent opens is only continuing: `send_to_session` to one of the chat's
own workers, and `manage_agent_loadout` status and stop. Starting a new worker
still needs this turn's wording or the tool setting. Policy `never` is
decided before any of this and stays absolute.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

GRANTED_KEY = "delegation_granted"
ENABLED_TOOLS_KEY = "delegation_tools_enabled"
CONTINUE_TOOLS = frozenset({"send_to_session", "manage_agent_loadout"})
# manage_agent_loadout actions that work with a worker that already exists.
_CONTINUE_ACTIONS = frozenset({"status", "stop", "poll", "wait", "check"})


def _delegation_tools() -> frozenset:
    from src.agent_loop import _DELEGATION_TOOLS

    return _DELEGATION_TOOLS


def record_intent(session_id: Optional[str], settings: Dict[str, Any], text: str) -> None:
    """Store what the person's latest words say about agents on the chat.

    ``settings`` is the caller's copy and is updated too, so the same turn sees
    the new value.
    """
    if not session_id:
        return
    from src.delegation_intent import delegation_prohibited, explicit_delegation_requested

    if explicit_delegation_requested(text):
        value: Optional[bool] = True
    elif delegation_prohibited(text):
        value = False
    else:
        return
    if settings.get(GRANTED_KEY) is value:
        return
    try:
        from core.database import update_session_settings

        if update_session_settings(session_id, {GRANTED_KEY: value}) is not None:
            settings[GRANTED_KEY] = value
    except Exception:
        logger.debug("delegation consent was not stored for %s", session_id, exc_info=True)


def child_workers(session_id: Optional[str], limit: int = 50) -> List[Tuple[str, str]]:
    """``(id, name)`` of the chats whose settings name ``session_id`` as parent."""
    if not session_id:
        return []
    try:
        from core.database import Session as DbSession, get_db_session

        with get_db_session() as db:
            rows = db.query(DbSession.id, DbSession.name, DbSession.settings_json).filter(
                DbSession.settings_json.contains(str(session_id))).limit(500).all()
        out = []
        for sid, name, raw in rows:
            try:
                parent = (json.loads(raw) if raw else {}).get("parent_session")
            except (TypeError, ValueError):
                continue
            if parent == session_id:
                out.append((sid, name or ""))
        return out[:limit]
    except Exception:
        logger.debug("child worker lookup failed for %s", session_id, exc_info=True)
        return []


def chat_may_continue(session_id: Optional[str], settings: Dict[str, Any]) -> bool:
    """Whether this chat has the person's consent to keep working with its agents."""
    granted = settings.get(GRANTED_KEY)
    if granted is False:
        return False
    return bool(granted) or bool(child_workers(session_id, limit=1))


def relax_gate(gated: Set[str], session_id: Optional[str], settings: Dict[str, Any]) -> Tuple[Set[str], Set[str]]:
    """``(still_gated, continue_only)`` after the chat's consent is applied."""
    gated = set(gated)
    if not gated:
        return gated, set()
    gated -= set(settings.get(ENABLED_TOOLS_KEY) or ())
    if gated & CONTINUE_TOOLS and chat_may_continue(session_id, settings):
        continue_only = gated & CONTINUE_TOOLS
        return gated - continue_only, continue_only
    return gated, set()


def _switched_on(settings: Dict[str, Any], tool: str) -> bool:
    """Whether the chat's own tool settings let it call ``tool``: not in its
    denylist, and in its allowlist when it has one."""
    from src.tool_policy import allowlist_permits

    if tool in (settings.get("disabled_tools") or ()):
        return False
    return allowlist_permits(tool, settings.get("tool_access"), settings.get("enabled_tools") or ())


def record_tool_settings_change(session_id: str, before: Dict[str, Any], after: Dict[str, Any]) -> None:
    """Note delegation tools the person switched on or off in the chat's tool settings.

    ``before`` and ``after`` are the chat's settings around a change a person
    made in the UI: removing a tool from the denylist, or adding it to a
    ``tool_access=selected`` allowlist, switches it on.
    """
    switched_on, switched_off = set(), set()
    for tool in _delegation_tools():
        was, now = _switched_on(before, tool), _switched_on(after, tool)
        if now and not was:
            switched_on.add(tool)
        elif was and not now:
            switched_off.add(tool)
    if not (switched_on or switched_off):
        return
    try:
        from core.database import get_session_settings, update_session_settings

        enabled = (set(get_session_settings(session_id).get(ENABLED_TOOLS_KEY) or ()) | switched_on) - switched_off
        update_session_settings(session_id, {ENABLED_TOOLS_KEY: sorted(enabled) or None})
    except Exception:
        logger.debug("delegation tool setting was not stored for %s", session_id, exc_info=True)


def own_worker_target(content: Any, session_id: Optional[str]) -> Tuple[str, bool]:
    """``(target id, is one of this chat's workers)`` for a send_to_session call."""
    from src.ai_interaction import get_session_manager
    from src.agent_tools.session_tools import _parse_send_args, resolve_session_ref

    target, _message, _mode = _parse_send_args(str(content or ""))
    if not target or target.lower() in ("new", "current"):
        return target, False
    try:
        manager = get_session_manager()
        if manager is not None:
            target = resolve_session_ref(manager, target) or target
    except Exception:
        pass
    return target, any(sid == target for sid, _name in child_workers(session_id))


def continuation_refusal(tool: str, content: Any, session_id: Optional[str], owner: Optional[str]) -> Optional[str]:
    """Why a call is not just continuing existing work, or None when it is."""
    if tool == "send_to_session":
        _target, own = own_worker_target(content, session_id)
        if own:
            return None
        return denial_message(tool, "explicit", detail=_own_workers_hint(session_id))
    if tool == "manage_agent_loadout":
        from src.tool_utils import _parse_tool_args

        try:
            args = _parse_tool_args(content)
        except ValueError:
            args = {}
        action = str((args if isinstance(args, dict) else {}).get("action") or "list").strip().lower()
        if action in _CONTINUE_ACTIONS:
            return None
        return denial_message(tool, "explicit", detail=_own_workers_hint(session_id))
    return None


def _own_workers_hint(session_id: Optional[str]) -> str:
    workers = child_workers(session_id, limit=5)
    if not workers:
        return ""
    listed = ", ".join(f"{sid} ({name})" if name else sid for sid, name in workers)
    return f" This turn can still resume or check this chat's own workers: {listed}."


def denial_message(tool: str, policy: str, detail: str = "") -> str:
    """What the model sees when the delegation gate refuses a tool."""
    if str(policy).lower() == "never":
        return (f"Tool '{tool}' is off because this chat's delegation policy is 'never'. Only the person can "
                "change it (Agents, this chat's loadout, Delegation). Do the work yourself or tell the person "
                "what needs another agent.")
    return (f"Tool '{tool}' is held back by this chat's delegation policy ('explicit'), not by a permission "
            "error: the person has not asked for other agents in this chat for this step. Ask the person to "
            "say they want it handed to an agent, or to switch the delegation policy to 'auto' or turn the tool "
            "on in the chat's tool settings." + detail)
