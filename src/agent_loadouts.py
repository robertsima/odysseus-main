"""Agent-authored worker loadouts.

A human can already define a reusable worker loadout in Settings › Workbench
and start one from the Agent Control Room. This module gives an agent the same
two abilities, under one extra rule:

    **An agent may not write itself a bigger loadout than it has.**

Every capability in a loadout an agent creates is intersected with the calling
chat's own effective policy — the ``sessions.settings_json`` record the Control
Room's loadout editor writes and :mod:`src.agent_loop` enforces. A chat denied
``bash`` cannot mint a "helper" loadout that has it. A chat without a private
vault grant cannot create one that reads private notes. A chat limited to two
parallel workers cannot define one allowed eight. Narrowings are reported back
to the agent so a clamped loadout is visible rather than silent.

The clamp is a narrowing rule, not a boundary of its own: what a worker may
actually do is still decided at execution time by its chat's stored policy
(:func:`src.agent_profiles.session_patch`) and by the owner baseline in
:func:`src.tool_security.owner_baseline_disabled_tools`. Clamping here stops an
agent from *authoring* an escalation; those two stop it from *running* one.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from src import agent_profiles

logger = logging.getLogger(__name__)

# Snapshot denylists are only a compatibility/early-filter optimization. The
# persisted positive tool_access/enabled_tools policy is authoritative, so a
# large ambient inventory must not prevent a tightly scoped worker starting.
MAX_DISABLED_TOOLS_IN_PROFILE = 200

_MEMORY_RANK = {"none": 0, "read": 1, "write": 2}
_SELECTION_RANK = {"none": 0, "selected": 1, "all": 2}
_MODEL_RANK = {"current": 0, "selected": 1, "all": 2}
_DELEGATION_RANK = {"never": 0, "explicit": 1, "auto": 2}
# Higher is stricter. A loadout may be at least as strict as its author, never
# looser: an agent that must ask before every change cannot author a worker
# that runs unattended.
_APPROVAL_STRICTNESS = {"auto": 0, "ask_risky": 1, "ask_all": 2}

# What an unconfigured chat gets. These mirror the defaults the Agents overview
# route reports, so a chat nobody has restricted is not treated as a chat that
# has been restricted to nothing.
_POLICY_DEFAULTS = {
    "memory_access": "write",
    "skill_access": "all",
    "model_access": "all",
    "delegation_policy": "explicit",
    "max_parallel_workers": 1,
}


def _rank_down(value: str, ceiling: str, ranks: Dict[str, int], label: str, notes: List[str]) -> str:
    """Clamp ``value`` to ``ceiling`` on the given ordering, noting any change."""
    if ranks.get(value, 0) <= ranks.get(ceiling, 0):
        return value
    notes.append(f"{label}: {value} → {ceiling} (the calling chat is limited to {ceiling})")
    return ceiling


def caller_policy(session_id: Optional[str], owner: Optional[str]) -> Dict[str, Any]:
    """The effective policy of the chat an agent is calling from."""
    from src.session_settings import effective_approval_mode, effective_worker_limit
    from src.tool_policy import known_tool_names
    from src.tool_security import owner_baseline_disabled_tools

    settings: Dict[str, Any] = {}
    if session_id:
        try:
            from core.database import get_session_settings

            settings = get_session_settings(session_id, strict=True) or {}
        except Exception as exc:
            logger.warning("loadout: could not read policy for %s: %s", session_id, exc)
            raise ValueError("Could not read the calling chat's policy; no loadout permissions granted") from exc
    known = set(known_tool_names())
    try:
        from src.tool_utils import get_mcp_manager
        manager = get_mcp_manager()
        mcp_tools = manager.get_all_tools() if manager else []
        known.update(t["qualified_name"] for t in mcp_tools if t.get("qualified_name"))
    except Exception:
        mcp_tools = []
    denied = agent_profiles.expand_tool_aliases(
        set(settings.get("disabled_tools") or []) | set(owner_baseline_disabled_tools(owner))
    )
    allowed_servers = settings.get("allowed_mcp_servers", ["*"])
    if isinstance(allowed_servers, list) and "*" not in allowed_servers:
        denied.update(t["qualified_name"] for t in mcp_tools
                      if t.get("qualified_name") and t.get("server_id") not in allowed_servers)
    if settings.get("tool_access") in {"selected", "none"}:
        enabled = agent_profiles.expand_tool_aliases(settings.get("enabled_tools") or []) if settings["tool_access"] == "selected" else set()
        denied.update(known - enabled)
    return {
        "allowed_tools": known - denied,
        "known_tools": known,
        "memory_access": settings.get("memory_access") or _POLICY_DEFAULTS["memory_access"],
        "skill_access": settings.get("skill_access") or _POLICY_DEFAULTS["skill_access"],
        "skill_names": set(settings.get("skill_names") or []),
        "model_access": settings.get("model_access") or _POLICY_DEFAULTS["model_access"],
        "allowed_models": set(settings.get("allowed_models") or []),
        "allowed_mcp_servers": list(settings.get("allowed_mcp_servers", ["*"]) or []),
        "private_vault_access": bool(settings.get("private_vault_access", False)),
        "delegation_policy": settings.get("delegation_policy") or _POLICY_DEFAULTS["delegation_policy"],
        "max_parallel_workers": effective_worker_limit(settings),
        "approval_mode": effective_approval_mode(settings),
        "session_model": str(settings.get("_model") or ""),
    }


def read_only_tools() -> frozenset:
    """The harness's read-only classification: plan mode's allowlist plus the
    read-side accessors it leaves out. It is the set a research specialist
    gets, so "read-only" means one thing for every kind of worker."""
    from src.agent_workflows import _READ_TOOLS

    return frozenset(_READ_TOOLS)


# Prefix of the narrowing note that means "this loadout has no tools at all".
# Callers match on it rather than re-deriving the intersection.
STARVED_NOTE = "tools: NONE of the requested tools are available to the calling chat"


def tool_starved(notes: List[str]) -> bool:
    """Did clamping leave a loadout that asked for tools with none?"""
    return any(str(note).startswith(STARVED_NOTE) for note in notes or [])


def unusable_reason(profile: Dict[str, Any]) -> Optional[str]:
    """Why a stored loadout cannot do tool-using work, or None.

    ``tool_access: "none"`` covers MCP too: MCP bindings live in
    ``enabled_tools`` as ``mcp__server__tool`` names, so a loadout with no
    tools has no MCP reach either.
    """
    if str(profile.get("tool_access") or "all") != "none":
        return None
    return (
        f"loadout {profile.get('name', '?')!r} grants no tools at all "
        "(tool_access is 'none'), so its worker can only produce prose — it cannot read, "
        "search, or verify anything"
    )


def _mcp_state() -> Tuple[Dict[str, Tuple[str, str]], Set[str]]:
    """({qualified: (server_id, connection status)}, deferred qualified names)."""
    try:
        from src.tool_utils import get_mcp_manager

        manager = get_mcp_manager()
        if manager is None:
            return {}, set()
        rows = {}
        for tool in manager.get_all_tools():
            name = tool.get("qualified_name")
            if name:
                status = str((manager.get_server_status(tool["server_id"]) or {}).get("status") or "unknown")
                rows[name] = (str(tool["server_id"]), status)
        try:
            deferred = set(manager.gated_tool_names())
        except Exception:
            deferred = set()
        return rows, deferred
    except Exception:
        logger.debug("loadout: MCP state unavailable for the capability matrix", exc_info=True)
        return {}, set()


def capability_matrix(requested_tools, required_tools, profile: Dict[str, Any],
                      policy: Dict[str, Any], owner: Optional[str] = None) -> Dict[str, Any]:
    """Every state a requested tool can be in, and why each missing one is missing.

    "Available" used to mean any of five things: the name exists, its MCP
    server is connected, the calling chat may grant it, the profile kept it,
    or its schema is sent every turn. A profile could save "successfully"
    while a tool its mission depended on was dropped by the clamp, and the
    worker then said the capability did not exist. ``required_tools`` names
    what the mission cannot do without; any of those denied is BLOCKED.
    """
    from src.private_access import tool_requires_private_grant
    from src.tool_security import owner_baseline_disabled_tools

    mcp, deferred_names = _mcp_state()
    requested = sorted({str(t) for t in (requested_tools or []) if t})
    required = sorted({str(t) for t in (required_tools or []) if t})
    wanted = sorted(set(requested) | set(required))
    known = set(policy.get("known_tools") or ()) | set(mcp)
    authorized = agent_profiles.expand_tool_aliases(policy.get("allowed_tools") or ())
    owner_denied = owner_baseline_disabled_tools(owner)
    selected = set(profile.get("enabled_tools") or []) if profile.get("tool_access") == "selected" else (
        set() if profile.get("tool_access") == "none" else set(wanted) & authorized)
    # Mirrors agent_profiles.session_patch: "all" is stored as ["*"] on the
    # worker chat whatever allowed_mcp_servers holds.
    server_limited = profile.get("mcp_access") in {"selected", "none"}
    permitted_servers = (set(profile.get("allowed_mcp_servers") or [])
                         if profile.get("mcp_access") == "selected" else set())

    def mcp_problem(tool):
        if tool not in mcp:
            return None
        server, status = mcp[tool]
        if server_limited and server not in permitted_servers:
            return f"MCP server {server} is not in this profile's allowed servers"
        if status != "connected":
            return f"MCP server {server} is {status}"
        return None

    denied, effective, conditional = [], [], []
    for tool in wanted:
        if tool in selected and not mcp_problem(tool):
            effective.append(tool)
            if tool_requires_private_grant(tool) and not profile.get("private_vault_access"):
                conditional.append({"tool": tool, "condition": (
                    "no private-vault grant: bash/python run only in the workspace sandbox; "
                    "other private-boundary tools are refused")})
            continue
        if tool not in known:
            reason, detail = "unknown", "no native tool or connected MCP tool has this name"
        elif tool in owner_denied:
            reason, detail = "owner_policy", "switched off for this user or by the operator"
        elif mcp_problem(tool):
            reason, detail = "mcp_server", mcp_problem(tool)
        elif tool not in authorized:
            reason, detail = "parent_policy", "the calling chat may not use it, so it cannot grant it"
        else:
            reason, detail = "not_requested", "not in this profile's enabled_tools"
        denied.append({"tool": tool, "reason": reason, "detail": detail})
    missing = [row["tool"] for row in denied if row["tool"] in required]
    return {
        "status": "BLOCKED" if missing else ("DEGRADED" if denied else "READY"),
        "requested": requested,
        "required": required,
        "known": [t for t in wanted if t in known],
        "connected": [t for t in wanted if t in known and (t not in mcp or mcp[t][1] == "connected")],
        "authorized": [t for t in wanted if t in authorized],
        "selected_for_profile": sorted(effective),
        "deferred_schema": sorted(set(effective) & deferred_names),
        "conditional": conditional,
        "denied": denied,
        "mission_critical_missing": missing,
    }


def _clamp_tools(prof: Dict[str, Any], policy: Dict[str, Any], notes: List[str]) -> None:
    known: Set[str] = policy["known_tools"]
    explicit_denied = agent_profiles.expand_tool_aliases(prof.get("disabled_tools") or [])
    if prof["tool_access"] == "none":
        wanted: Set[str] = set()
    elif prof["tool_access"] == "selected":
        wanted = set(prof.get("enabled_tools") or []) - explicit_denied
    else:
        wanted = known - explicit_denied
    granted = wanted & agent_profiles.expand_tool_aliases(policy["allowed_tools"])
    refused = sorted(wanted - granted)
    if wanted and not granted:
        # Every requested tool was refused, so line 140 below turns this into
        # `tool_access: "none"` -- a worker that starts, discovers it can do
        # nothing, and says so. That is not a narrowing, it is the loadout
        # failing to exist, and the callers check for this marker to refuse
        # rather than store or start it. (2026-09-17: a research loadout asked
        # for web tools from a chat that had none, was stored with zero tools,
        # and every worker it started opened with "I'm blocked".)
        notes.append(
            STARVED_NOTE + f": {', '.join(refused[:8])}{'…' if len(refused) > 8 else ''}"
        )
    elif refused:
        notes.append(
            f"tools: dropped {len(refused)} the calling chat cannot use itself "
            f"({', '.join(refused[:8])}{'…' if len(refused) > 8 else ''})"
        )
    complement = sorted((known - agent_profiles.expand_tool_aliases(granted)) | set(prof.get("disabled_tools") or []))
    if len(complement) > MAX_DISABLED_TOOLS_IN_PROFILE:
        # Do not truncate an authoritative denylist or reject a one-tool grant
        # because hundreds of unrelated MCP tools exist. Keep explicit denials;
        # selected/none below rejects everything outside the positive bindings,
        # including tools that connect after this profile is stored.
        complement = list(prof.get("disabled_tools") or [])
    prof["tool_access"] = "selected" if granted else "none"
    prof["enabled_tools"] = sorted(granted)
    # Retain the cheap snapshot when it fits, plus explicit denials regardless
    # of inventory size. Fresh profile children must persist selected/none
    # successfully before running; that positive policy binds the first turn.
    prof["disabled_tools"] = complement


def _clamp_models(prof: Dict[str, Any], policy: Dict[str, Any], notes: List[str]) -> None:
    prof["model_access"] = _rank_down(prof["model_access"], policy["model_access"], _MODEL_RANK,
                                      "model_access", notes)
    if policy["model_access"] == "current":
        if prof.get("model") or prof.get("model_fallbacks") or prof.get("allowed_models"):
            notes.append("models: cleared (the calling chat may not switch model)")
        prof["model"] = ""
        prof["model_fallbacks"] = []
        prof["allowed_models"] = []
        return
    if policy["model_access"] == "all":
        return
    permitted = policy["allowed_models"]
    for field in ("allowed_models", "model_fallbacks"):
        kept = sorted(set(prof.get(field) or []) & permitted)
        if len(kept) != len(prof.get(field) or []):
            notes.append(f"{field}: narrowed to models the calling chat may use")
        prof[field] = kept
    if prof.get("model") and prof["model"] not in permitted:
        notes.append(f"model: cleared ({prof['model']!r} is outside the calling chat's model list)")
        prof["model"] = ""


def _clamp_mcp(prof: Dict[str, Any], policy: Dict[str, Any], notes: List[str]) -> None:
    allowed = policy["allowed_mcp_servers"]
    if "*" in allowed:
        return
    permitted = set(allowed)
    if prof["mcp_access"] == "all":
        prof["mcp_access"] = "selected" if permitted else "none"
        prof["allowed_mcp_servers"] = sorted(permitted)
        notes.append("mcp_access: all → the calling chat's own connection list")
        return
    kept = sorted(set(prof.get("allowed_mcp_servers") or []) & permitted)
    if len(kept) != len(prof.get("allowed_mcp_servers") or []):
        notes.append("allowed_mcp_servers: narrowed to connections the calling chat may use")
    prof["allowed_mcp_servers"] = kept
    if not kept:
        prof["mcp_access"] = "none"


def clamp(requested: Dict[str, Any], policy: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Normalise a requested loadout and narrow it to ``policy``.

    Returns the storable profile and a list of human-readable narrowings.
    Raises ``ValueError`` when the request itself is malformed.
    """
    prof = agent_profiles.validate_profiles([requested])[0]
    notes: List[str] = []

    _clamp_tools(prof, policy, notes)
    _clamp_models(prof, policy, notes)
    _clamp_mcp(prof, policy, notes)

    prof["memory_access"] = _rank_down(prof["memory_access"], policy["memory_access"],
                                       _MEMORY_RANK, "memory_access", notes)
    prof["skill_access"] = _rank_down(prof["skill_access"], policy["skill_access"],
                                      _SELECTION_RANK, "skill_access", notes)
    if policy["skill_access"] == "none":
        prof["skill_names"] = []
    elif policy["skill_access"] == "selected":
        kept = sorted(set(prof.get("skill_names") or []) & policy["skill_names"])
        if len(kept) != len(prof.get("skill_names") or []):
            notes.append("skill_names: narrowed to skills the calling chat may use")
        prof["skill_names"] = kept
        if not kept and prof["skill_access"] == "selected":
            prof["skill_access"] = "none"

    prof["delegation_policy"] = _rank_down(prof["delegation_policy"], policy["delegation_policy"],
                                           _DELEGATION_RANK, "delegation_policy", notes)
    if prof["max_parallel_workers"] > policy["max_parallel_workers"]:
        notes.append(f"max_parallel_workers: {prof['max_parallel_workers']} → {policy['max_parallel_workers']}")
        prof["max_parallel_workers"] = policy["max_parallel_workers"]

    if prof["private_vault_access"] and not policy["private_vault_access"]:
        notes.append("private_vault_access: denied (the calling chat has no private-vault grant)")
        prof["private_vault_access"] = False

    floor = policy["approval_mode"]
    current = prof["approval_mode"]
    if current == "inherit":
        pass  # inheriting keeps the worker chat's own resolved mode.
    elif _APPROVAL_STRICTNESS.get(current, 0) < _APPROVAL_STRICTNESS.get(floor, 0):
        notes.append(f"approval_mode: {current} → {floor} (may not be looser than the calling chat)")
        prof["approval_mode"] = floor

    # Re-validate so what is stored is exactly what validate_profiles accepts.
    return agent_profiles.validate_profiles([prof])[0], notes


def _write(profiles: List[Dict[str, Any]]) -> None:
    from src.settings import load_settings, save_settings

    settings = load_settings()
    settings["agent_profiles"] = agent_profiles.validate_profiles(profiles)
    save_settings(settings)


def save(profile: Dict[str, Any], *, replace: bool) -> Dict[str, Any]:
    """Insert or update one loadout in the ``agent_profiles`` setting."""
    existing = agent_profiles.load_profiles()
    key = profile["name"].casefold()
    index = next((i for i, item in enumerate(existing) if item["name"].casefold() == key), None)
    if index is None:
        if len(existing) >= agent_profiles.MAX_PROFILES:
            raise ValueError(f"at most {agent_profiles.MAX_PROFILES} loadouts; delete one first")
        existing.append(profile)
    elif replace:
        existing[index] = profile
    else:
        raise ValueError(f"a loadout named {profile['name']!r} already exists; use action='update'")
    _write(existing)
    return profile


def delete(name: str) -> bool:
    key = str(name or "").strip().casefold()
    existing = agent_profiles.load_profiles()
    remaining = [item for item in existing if item["name"].casefold() != key]
    if len(remaining) == len(existing):
        return False
    _write(remaining)
    return True


def summarize(profile: Dict[str, Any]) -> Dict[str, Any]:
    """The compact view an agent gets back — policy, not prompt text."""
    return {
        "name": profile["name"],
        "description": profile["description"],
        "model": profile["model"] or "inherit",
        "tool_access": profile["tool_access"],
        "tools": profile["enabled_tools"] if profile["tool_access"] == "selected" else profile["tool_access"],
        "memory_access": profile["memory_access"],
        "skill_access": profile["skill_access"],
        "skills": profile["skill_names"],
        "model_access": profile["model_access"],
        "allowed_models": profile["allowed_models"],
        "mcp_access": profile["mcp_access"],
        "allowed_mcp_servers": profile["allowed_mcp_servers"],
        "private_vault_access": profile["private_vault_access"],
        "delegation_policy": profile["delegation_policy"],
        "approval_mode": profile["approval_mode"],
        "max_parallel_workers": profile["max_parallel_workers"],
        "max_rounds": profile["max_rounds"],
        **{key: profile[key] for key in _VOICE_FIELDS if profile.get(key) not in (None, "")},
    }


# Optional voice/sampling fields: shown by summarize() only when the loadout
# sets them, so ``get`` reports what a chat under it actually runs with.
_VOICE_FIELDS = ("persona_name", "temperature", "max_tokens", "reasoning_effort")


def discovery_summary(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Small row for loadout discovery.

    ``tool_access=selected`` can contain a large allow-list. Repeating that
    list for every saved loadout made the ordinary ``list`` call much larger
    than the decision it supports (pick a name, then inspect it). ``get``
    still returns :func:`summarize` and instructions for one chosen loadout.
    """
    selected = profile["enabled_tools"] if profile["tool_access"] == "selected" else []
    return {
        "name": profile["name"],
        "description": profile["description"],
        "model": profile["model"] or "inherit",
        "tool_access": profile["tool_access"],
        "tool_count": len(selected) if profile["tool_access"] == "selected" else None,
        "memory_access": profile["memory_access"],
        "delegation_policy": profile["delegation_policy"],
        "max_parallel_workers": profile["max_parallel_workers"],
        "max_rounds": profile["max_rounds"],
    }
