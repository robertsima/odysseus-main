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

# `validate_profiles` truncates a profile's name lists rather than rejecting
# them. For `disabled_tools` that would be a silent *under*-denial, so a clamp
# that would need more entries than fit is refused instead.
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
    from src.session_settings import effective_approval_mode
    from src.tool_policy import allowlist_permits, known_tool_names
    from src.tool_security import owner_baseline_disabled_tools

    settings: Dict[str, Any] = {}
    if session_id:
        try:
            from core.database import get_session_settings

            settings = get_session_settings(session_id) or {}
        except Exception as exc:
            logger.warning("loadout: could not read policy for %s: %s", session_id, exc)
    known = set(known_tool_names())
    denied = set(settings.get("disabled_tools") or []) | set(owner_baseline_disabled_tools(owner))
    # The caller's own allowlist has to be read here too, not just its
    # denylist. Once a chat's policy is stored as `tool_access`/`enabled_tools`
    # rather than as a pre-inverted denylist, a chat narrowed to three tools has
    # an *empty* `disabled_tools` — so an authoring clamp that looked only at
    # the denylist would see an unrestricted caller and let it mint a worker
    # with every tool. That is the escalation this module exists to stop.
    caller_access = settings.get("tool_access") or "all"
    caller_enabled = list(settings.get("enabled_tools") or [])
    return {
        "allowed_tools": {
            name for name in known
            if name not in denied and allowlist_permits(name, caller_access, caller_enabled)
        },
        "known_tools": known,
        "tool_access": caller_access,
        "enabled_tools": caller_enabled,
        "denied_tools": denied,
        "memory_access": settings.get("memory_access") or _POLICY_DEFAULTS["memory_access"],
        "skill_access": settings.get("skill_access") or _POLICY_DEFAULTS["skill_access"],
        "skill_names": set(settings.get("skill_names") or []),
        "model_access": settings.get("model_access") or _POLICY_DEFAULTS["model_access"],
        "allowed_models": set(settings.get("allowed_models") or []),
        "allowed_mcp_servers": list(settings.get("allowed_mcp_servers") or ["*"]),
        "private_vault_access": bool(settings.get("private_vault_access", False)),
        "delegation_policy": settings.get("delegation_policy") or _POLICY_DEFAULTS["delegation_policy"],
        "max_parallel_workers": int(settings.get("max_parallel_workers", _POLICY_DEFAULTS["max_parallel_workers"])),
        "approval_mode": effective_approval_mode(settings),
        "session_model": str(settings.get("_model") or ""),
    }


def _caller_may_grant_mcp(entry: str, policy: Dict[str, Any]) -> bool:
    """Whether the calling chat may write an MCP grant into a worker's allowlist.

    An `enabled_tools` entry can now name an MCP tool (`mcp__email__list_emails`),
    a whole server (`mcp__email__*`) or every server (`mcp__*`). The clamp rule
    is unchanged — never author more than you hold — so a grant is kept only
    when the caller could make the same call itself: its own allowlist permits
    it and its own connection list covers the server.
    """
    from src.tool_policy import (
        ALL_MCP_WILDCARD,
        allowlist_is_active,
        allowlist_permits,
        split_mcp_tool_name,
    )

    if entry in (policy.get("denied_tools") or set()):
        return False
    servers = policy.get("allowed_mcp_servers") or ["*"]
    any_server = "*" in servers
    access = policy.get("tool_access", "all")
    enabled = set(policy.get("enabled_tools") or [])

    if entry == ALL_MCP_WILDCARD:
        # "every connected server" may only be handed on by a caller that has
        # every connected server itself.
        return any_server and (not allowlist_is_active(access) or ALL_MCP_WILDCARD in enabled)
    if entry.startswith("mcp__") and entry.endswith("__*"):
        server = entry[len("mcp__"):-len("__*")]
        if not server or not (any_server or server in set(servers)):
            return False
        # A whole-server grant needs the caller to hold the whole server, not
        # merely one tool on it.
        return (
            not allowlist_is_active(access)
            or ALL_MCP_WILDCARD in enabled
            or entry in enabled
        )
    parts = split_mcp_tool_name(entry)
    if not parts:
        return False
    if not (any_server or parts[0] in set(servers)):
        return False
    return allowlist_permits(entry, access, enabled)


def _caller_mcp_grants(policy: Dict[str, Any]) -> Set[str]:
    """Wildcard entries standing for the calling chat's own MCP reach.

    ``tool_access: "all"`` means "everything the calling chat has", and the
    calling chat's MCP tools are part of that — but those names are generated
    at runtime and carry a per-server id, so they cannot be enumerated into an
    allowlist. The reach is carried as a wildcard instead.
    """
    servers = policy.get("allowed_mcp_servers")
    if servers is None:
        servers = ["*"]
    if "*" in servers:
        return {"mcp__*"}
    return {f"mcp__{str(s).strip()}__*" for s in servers if str(s).strip()}


def _clamp_tools(prof: Dict[str, Any], policy: Dict[str, Any], notes: List[str]) -> None:
    from src.tool_policy import denied_by_allowlist, is_mcp_tool_name

    known: Set[str] = policy["known_tools"]
    explicit_denied = set(prof.get("disabled_tools") or [])
    if prof["tool_access"] == "none":
        wanted: Set[str] = set()
    elif prof["tool_access"] == "selected":
        wanted = set(prof.get("enabled_tools") or []) - explicit_denied
    else:
        wanted = (known | _caller_mcp_grants(policy)) - explicit_denied

    def _mcp_shaped(name: str) -> bool:
        return is_mcp_tool_name(name) or (name.startswith("mcp__") and name.endswith("*"))

    native_wanted = {name for name in wanted if not _mcp_shaped(name)}
    mcp_wanted = wanted - native_wanted
    granted = (native_wanted & policy["allowed_tools"]) | {
        name for name in mcp_wanted if _caller_may_grant_mcp(name, policy)
    }
    refused = sorted(wanted - granted)
    if refused:
        notes.append(
            f"tools: dropped {len(refused)} the calling chat cannot use itself "
            f"({', '.join(refused[:8])}{'…' if len(refused) > 8 else ''})"
        )
    # Belt-and-braces only, and built from the shared inversion rather than a
    # second hand-written subtraction: `launch_worker()` hands the profile's own
    # `disabled_tools` straight to `run_headless()`. The allowlist itself is
    # what binds, through the worker chat's stored `tool_access`/`enabled_tools`
    # (`agent_profiles.session_patch`), so this list going stale can no longer
    # widen anything — it can only deny.
    complement = sorted(
        denied_by_allowlist(known, tool_access="selected", enabled_tools=granted)
    )
    if len(complement) > MAX_DISABLED_TOOLS_IN_PROFILE:
        # Never store a truncated denylist: the entries that fell off the end
        # would read back as "allowed".
        raise ValueError(
            "too many tools to deny explicitly for this loadout; narrow enabled_tools instead"
        )
    prof["tool_access"] = "selected" if granted else "none"
    prof["enabled_tools"] = sorted(granted)
    # Written out as well as implied: launch_worker() hands the profile's own
    # disabled_tools straight to run_headless(), so the clamp has to be in this
    # field to bind the very first turn, not only in the chat's saved policy.
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


def _align_mcp_with_tool_allowlist(prof: Dict[str, Any], notes: List[str]) -> None:
    """Make the stored connection list say what the tool allowlist will permit.

    The tool allowlist is the gate that decides; `allowed_mcp_servers` is what
    the prompt advertises. A loadout narrowed to named tools that still listed
    every connected server would advertise servers whose every tool the gate
    rejects — the phantom-tool failure `docs/design-patterns.md` names under
    "the offer and the enforcement must agree". Report the narrowing rather
    than performing it silently.
    """
    from src.tool_policy import reconcile_tool_and_mcp_access

    before = list(prof.get("allowed_mcp_servers") or [])
    enabled, after = reconcile_tool_and_mcp_access(
        tool_access=prof.get("tool_access", "all"),
        enabled_tools=prof.get("enabled_tools") or [],
        mcp_access=prof.get("mcp_access", "all"),
        allowed_mcp_servers=before,
    )
    prof["enabled_tools"] = enabled
    if sorted(before) == sorted(after):
        return
    prof["allowed_mcp_servers"] = after
    prof["mcp_access"] = "selected" if after and "*" not in after else ("all" if after else "none")
    notes.append(
        "allowed_mcp_servers: narrowed to the servers this loadout's tool "
        "allowlist can reach (add mcp__<server>__* or mcp__* to enabled_tools to widen)"
    )


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
    _align_mcp_with_tool_allowlist(prof, notes)

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
    }
