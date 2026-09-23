"""The link between a crew member and a named agent profile (loadout).

Odysseus grew two unconnected ways to say "an AI agent with a role":

* ``crew_members`` (``core.database.CrewMember``) — a persona row with its own
  ``personality``, ``model``/``endpoint_url``, ``enabled_tools`` allowlist and
  a pinned chat ``session_id``.  Until now its ``personality`` and
  ``enabled_tools`` were read **only** by ``src/task_scheduler.py``, i.e. only
  when a scheduled task ran as that crew member.  Chatting with a scoped crew
  member applied no tool scoping at all.
* ``agent_profiles`` (``src/agent_profiles.py``, the ``agent_profiles``
  setting) — a full worker loadout: instructions, model access, tool/skill/MCP
  allowlists, memory and vault access, approvals, delegation, round budget.

``crew_members.agent_profile`` now names a profile, and this module is the one
place that says what that link means.  Both surfaces import it so the rule
cannot drift: the chat path (``routes/chat_routes.py`` for policy,
``routes/chat_helpers.py`` for the role prompt) and the scheduler path
(``src/task_scheduler.py``).


Precedence — what wins when a crew member has BOTH its own fields and a profile
------------------------------------------------------------------------------

1. **Tool policy composes restrictively.**  The effective deny set is the union
   of the profile's denials and the inversion of the crew's own
   ``enabled_tools`` allowlist — that is, the two allowlists *intersect*.
   Neither side can widen the other, so attaching a profile to a narrowly
   scoped crew member can only ever remove capability.

   This is a policy decision, and policy fails closed
   (``docs/design-patterns.md``).  The obvious alternative — "the profile wins
   outright" — would silently hand a crew member scoped to three tools every
   tool the profile allows, and the widening would be invisible at the point
   where someone assigned the role.

2. **Every other runtime policy comes from the profile alone.**  Memory, skill,
   model and MCP access, private-vault reads, approval mode, delegation policy
   and worker concurrency are exactly the keys
   ``src.agent_profiles.session_patch()`` writes, and the crew row has no
   competing field for any of them, so there is nothing to reconcile.

3. **Prompt text composes; it does not override.**  ``personality`` first (who
   this agent is), then the profile's ``instructions`` (how this role works).
   Both are hand-written by the same user for the same agent; dropping one of
   them is the worse failure, and unlike a capability grant, text carries no
   fail-closed hazard.

4. **No linked profile changes nothing.**  Every function here returns "no
   opinion" when the crew row is absent or its ``agent_profile`` is empty, so
   an install that never assigns a role behaves exactly as before.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "crew_for_session",
    "profile_for_crew",
    "crew_enabled_tools",
    "crew_disabled_tools",
    "merge_crew_disabled_tools",
    "effective_disabled_tools",
    "role_system_prompt",
    "session_policy_patch",
    "apply_crew_profile_to_session",
    "role_system_prompt_for_session",
]


# ── Lookup ─────────────────────────────────────────────────────────────── #

def crew_for_session(session_id: str):
    """The ``CrewMember`` row this chat belongs to, or ``None``.

    Resolved through ``sessions.crew_member_id`` (both the scheduler's seeded
    assistant and the agents UI write that link).  Never raises: an unreadable
    database means "this chat has no crew role", which is the same answer as a
    plain chat.
    """
    if not session_id:
        return None
    try:
        from core.database import CrewMember, Session as DbSession, get_db_session

        with get_db_session() as db:
            crew_id = (
                db.query(DbSession.crew_member_id)
                .filter(DbSession.id == session_id)
                .scalar()
            )
            if not crew_id:
                return None
            crew = db.query(CrewMember).filter(CrewMember.id == crew_id).first()
            if crew is not None:
                db.expunge(crew)
            return crew
    except Exception:
        logger.debug("crew_for_session(%s) failed", session_id, exc_info=True)
        return None


def profile_for_crew(crew) -> Optional[Dict[str, Any]]:
    """The agent profile this crew member names, or ``None``.

    ``None`` also covers "names a profile that no longer exists" — a deleted
    loadout must not silently fall back to some other loadout, and the crew's
    own fields still apply on their own.
    """
    name = str(getattr(crew, "agent_profile", None) or "").strip()
    if not name:
        return None
    try:
        from src import agent_profiles

        profile = agent_profiles.get_profile(name)
    except Exception:
        logger.debug("profile_for_crew(%r) failed", name, exc_info=True)
        return None
    if profile is None:
        logger.warning(
            "crew member %r names agent profile %r, which no longer exists",
            getattr(crew, "name", None), name,
        )
    return profile


# ── Tool policy (precedence rule 1) ────────────────────────────────────── #

def crew_enabled_tools(crew) -> Optional[List[str]]:
    """The crew's own ``enabled_tools`` allowlist, or ``None`` for "unscoped".

    The column is JSON text — a list of names, or the string ``"all"``.  An
    empty list, ``"all"``, unparseable JSON and a NULL column all mean the same
    thing here: this crew member states no tool allowlist of its own.
    """
    raw = getattr(crew, "enabled_tools", None)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except Exception:
        return None
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return [item for item in value if item.strip()]
    return None


def crew_disabled_tools(crew) -> Set[str]:
    """The crew's allowlist expressed as a deny set (``{}`` when unscoped).

    The inversion is the shared one (``src.tool_policy.denied_by_allowlist``
    over ``live_tool_names()``), not a local one over
    ``BUILTIN_TOOL_DESCRIPTIONS``.  Inverting against a builtin-only registry
    is what let a crew member scoped to a handful of tools still reach every
    tool of every connected MCP server: no MCP name was in the universe, so no
    MCP name was ever denied.  Deferring to ``allowed_mcp_servers`` did not
    cover it either — a profile carries ``mcp_access="all"`` by default and
    nobody chose it.  One definition, imported: this is the same call the
    scheduler, ``agent_profiles`` and ``agent_loadouts`` make.
    """
    enabled = crew_enabled_tools(crew)
    if not enabled:
        return set()
    try:
        from src.tool_policy import denied_by_allowlist, live_tool_names

        return denied_by_allowlist(
            live_tool_names(), tool_access="selected", enabled_tools=enabled
        )
    except Exception:
        # Fail closed is not available here — an empty deny set is "unscoped",
        # and a crew member the registry cannot be read for must not silently
        # become unrestricted. Re-raise so the caller's own guard records it.
        raise


def merge_crew_disabled_tools(crew, disabled: Any) -> List[str]:
    """Union ``disabled`` with the crew's own allowlist inversion.

    Union of denies is intersection of allows: this is precedence rule 1, and
    it is the only function that implements it.  Both the chat path and the
    scheduler path call it so the same crew member is scoped identically
    whether it is answering a message or running a scheduled task.
    """
    return sorted(set(disabled or ()) | crew_disabled_tools(crew))


def effective_disabled_tools(crew) -> Set[str]:
    """Everything a crew member may not call, link or no link.

    With a linked profile that is the profile's denials unioned with the crew's
    own allowlist inversion; without one it is just the inversion.  Callers that
    already have a deny set of their own (the scheduler merges the operator's
    global ``disabled_tools``, chat merges a dozen gates) union this into it.
    """
    if crew is None:
        return set()
    patch = session_policy_patch(crew)
    if patch:
        # Through the single reader, not `patch["disabled_tools"]`: a profile
        # with an allowlist stores `tool_access`/`enabled_tools` and leaves the
        # deny field holding only its *extra* denials, so reading that field
        # alone drops the allowlist and hands this crew member every tool the
        # role was scoped away from.
        from src.session_settings import stored_disabled_tools

        return stored_disabled_tools(patch)
    return crew_disabled_tools(crew)


# ── Prompt text (precedence rule 3) ────────────────────────────────────── #

def role_system_prompt(crew, profile: Optional[Dict[str, Any]] = None) -> str:
    """``personality`` then the profile's ``instructions`` ("" when neither)."""
    if crew is None:
        return ""
    if profile is None:
        profile = profile_for_crew(crew)
    parts = [
        str(getattr(crew, "personality", None) or "").strip(),
        str((profile or {}).get("instructions") or "").strip(),
    ]
    return "\n\n".join(part for part in parts if part)


# ── Applying the link to a chat ────────────────────────────────────────── #

def session_policy_patch(crew, profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The session-settings patch a crew member's linked profile implies.

    Delegates to :func:`src.agent_profiles.session_patch` — the same function
    that gives a delegated worker its loadout — and then applies precedence
    rule 1 on top of whatever deny set it produced.  ``{}`` when the crew names
    no (existing) profile, so callers can treat "no link" as "no change".
    """
    if crew is None:
        return {}
    if profile is None:
        profile = profile_for_crew(crew)
    if not profile:
        return {}
    from src.agent_profiles import session_patch

    patch = dict(session_patch(profile))
    # Only narrow when the crew states an allowlist of its own; otherwise leave
    # whatever session_patch produced exactly as it produced it, so this stays
    # a caller of that function rather than a second opinion about how a
    # loadout is stored.
    if crew_disabled_tools(crew):
        patch["disabled_tools"] = merge_crew_disabled_tools(crew, patch.get("disabled_tools"))
    return patch


def apply_crew_profile_to_session(
    session_id: str, crew: Optional[Any] = None
) -> Tuple[Optional[Any], Dict[str, Any]]:
    """Persist the crew's linked-profile policy onto its chat's settings.

    Returns ``(crew, settings)`` where ``settings`` is the chat's settings dict
    after the patch (or as-read when there is nothing to apply), so the caller
    needs no second read.  ``crew`` may be passed in by callers that already
    know it and whose target chat is not the crew's own pinned session — the
    scheduler runs a crew member's task in that task's chat.

    The patch is *persisted* rather than applied in-flight on purpose.  The
    chat route is not the only reader of a chat's settings —
    ``src/agent_loop.py``, ``src/tool_execution.py``, ``src/private_access.py``
    and ``src/headless_agent.py`` each re-read them through
    ``get_session_settings`` during a turn — so writing the policy where they
    all look is what makes the role impossible to bypass by entering the loop
    another way.  ``session_patch`` is deterministic in the profile, so the
    write is idempotent.
    """
    from core.database import get_session_settings, update_session_settings

    if crew is None:
        crew = crew_for_session(session_id)
    patch = session_policy_patch(crew)
    if not patch:
        return crew, (get_session_settings(session_id) if session_id else {})
    merged = update_session_settings(session_id, patch)
    if merged is None:
        # The write failed (missing row, locked database).  Fall back to the
        # stored settings merged in memory so this turn is still scoped — a
        # failed write must not mean an unscoped agent.
        merged = dict(get_session_settings(session_id) or {})
        for key, value in patch.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
    return crew, merged


def role_system_prompt_for_session(session_id: str) -> str:
    """The composed role prompt for a chat, or "" when it has no linked role.

    Deliberately gated on the crew naming a profile: a crew member that has
    only ever had a ``personality`` keeps behaving exactly as it did, because
    chat has never injected that field and quietly starting to would change
    every existing assistant chat.  Assigning a role is the opt-in.
    """
    crew = crew_for_session(session_id)
    profile = profile_for_crew(crew) if crew is not None else None
    if not profile:
        return ""
    return role_system_prompt(crew, profile)
