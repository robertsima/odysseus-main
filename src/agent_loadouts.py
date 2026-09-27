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
    from src.tool_policy import allowlist_permits, denied_by_allowlist, known_tool_names
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
    # The caller's MCP reach is part of what it may hand on, so the qualified
    # names of every connected tool join the universe the clamp works over.
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
    # The caller's own allowlist has to be read here too, not just its
    # denylist. Once a chat's policy is stored as `tool_access`/`enabled_tools`
    # rather than as a pre-inverted denylist, a chat narrowed to three tools has
    # an *empty* `disabled_tools` -- so an authoring clamp that looked only at
    # the denylist would see an unrestricted caller and let it mint a worker
    # with every tool. That is the escalation this module exists to stop.
    #
    # Inverted through `denied_by_allowlist`, not by subtracting the enabled
    # names: `known` now holds MCP qualified names, and an allowlist grants
    # those by shape (`mcp__<server>__*`, `mcp__*`). A literal subtraction would
    # deny every tool of a server the caller was deliberately given whole.
    caller_access = settings.get("tool_access") or "all"
    caller_enabled = list(settings.get("enabled_tools") or [])
    if caller_access in {"selected", "none"}:
        denied.update(denied_by_allowlist(
            known, tool_access=caller_access, enabled_tools=caller_enabled,
        ))
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
        "allowed_mcp_servers": list(settings.get("allowed_mcp_servers", ["*"]) or []),
        "private_vault_access": bool(settings.get("private_vault_access", False)),
        "delegation_policy": settings.get("delegation_policy") or _POLICY_DEFAULTS["delegation_policy"],
        "max_parallel_workers": effective_worker_limit(settings),
        "approval_mode": effective_approval_mode(settings),
        "session_model": str(settings.get("_model") or ""),
        # How far below a person's chat the caller is. What it starts runs one
        # level deeper, and preflight has to judge the loadout at that depth.
        "worker_depth": _caller_depth(session_id),
    }


def _caller_depth(session_id: Optional[str]) -> int:
    if not session_id:
        return 0
    try:
        from src.headless_agent import worker_depth

        return worker_depth(session_id)
    except Exception:
        logger.debug("loadout: worker depth unavailable for %s", session_id, exc_info=True)
        return 0


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


def stale_mcp_grants(profile: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, List[str]]]:
    """MCP tools a loadout names that its (connected) server no longer has.

    ``{server_id: {"missing": [...], "present": [...]}}`` for every server that
    is connected right now and on which the loadout names at least one exact
    tool that server does not offer. Wildcard grants cannot go stale and are
    ignored, and so is a server with no tools listed at all — that is a
    connection problem, which the launcher reports separately.

    On 2026-09-23 the Penpot Product Designer loadout named the plugin Penpot
    MCP's four tools (`execute_code`, `high_level_overview`, ...) on server
    c5ec6d7a, which by then was a different 81-tool Penpot server. The
    allowlist permitted names nothing had, so the worker started with no Penpot
    tool at all and reported the job impossible. Nothing said why.
    """
    if not profile or profile.get("tool_access") != "selected":
        return {}
    from src.tool_policy import split_mcp_tool_name

    rows, _deferred = _mcp_state()
    offered: Dict[str, Set[str]] = {}
    for qualified, (server, _status) in rows.items():
        offered.setdefault(server, set()).add(qualified)
    out: Dict[str, Dict[str, List[str]]] = {}
    for entry in profile.get("enabled_tools") or []:
        parts = split_mcp_tool_name(entry)
        if not parts or parts[1] == "*":
            continue
        server = parts[0]
        if server not in offered:
            continue
        bucket = out.setdefault(server, {"missing": [], "present": []})
        bucket["present" if entry in offered[server] else "missing"].append(entry)
    return {server: b for server, b in out.items() if b["missing"]}


def worker_withheld_tools(profile: Dict[str, Any], caller_depth: int = 0) -> Dict[str, str]:
    """Tools this loadout grants that a worker started from the caller won't get.

    ``{tool: why}``. A worker runs one level below the chat that starts it and
    loses the launchers :func:`src.headless_agent.blocked_at_depth` names for
    that depth. The loadout's allowlist still says it has them, so without
    this preflight read READY for a worker that would be denied them.
    """
    from src.headless_agent import blocked_at_depth, withheld_reason

    if profile.get("tool_access") != "selected":
        return {}
    blocked = blocked_at_depth(int(caller_depth or 0) + 1, profile.get("delegation_policy"))
    return {tool: withheld_reason(tool)
            for tool in sorted(set(profile.get("enabled_tools") or []) & blocked)}


# What a worker needs to do repository work end to end: see status, diffs and
# history (manage_git), and commit on a branch and ask a human to publish
# (manage_agent_worktree). A loadout that edits code without them can change
# files and nothing else.
REPOSITORY_TOOLS = ("manage_git", "manage_agent_worktree")
_CODE_EDIT_TOOLS = frozenset({"edit_file", "apply_patch"})


def missing_repository_tools(profile: Optional[Dict[str, Any]]) -> List[str]:
    """Repository tools a selected-tools loadout does not bind (``[]`` otherwise)."""
    if not profile or profile.get("tool_access") != "selected":
        return []
    enabled = set(profile.get("enabled_tools") or []) - set(profile.get("disabled_tools") or [])
    return [tool for tool in REPOSITORY_TOOLS if tool not in enabled]


def edits_code(profile: Optional[Dict[str, Any]]) -> bool:
    return bool(profile) and bool(set(profile.get("enabled_tools") or []) & _CODE_EDIT_TOOLS)


def capability_matrix(requested_tools, required_tools, profile: Dict[str, Any],
                      policy: Dict[str, Any], owner: Optional[str] = None,
                      *, as_worker: bool = False) -> Dict[str, Any]:
    """Every state a requested tool can be in, and why each missing one is missing.

    "Available" used to mean any of five things: the name exists, its MCP
    server is connected, the calling chat may grant it, the profile kept it,
    or its schema is sent every turn. A profile could save "successfully"
    while a tool its mission depended on was dropped by the clamp, and the
    worker then said the capability did not exist. ``required_tools`` names
    what the mission cannot do without; any of those denied is BLOCKED.

    Two more ways a bound tool is not callable are always checked: a name the
    loadout's own ``disabled_tools`` also lists (the denial wins), and, with
    ``as_worker`` (preflight judges the loadout as the worker it starts), a
    tool a worker at that depth is never given.
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

    expand_disabled = agent_profiles.expand_tool_aliases(profile.get("disabled_tools") or [])
    withheld = worker_withheld_tools(profile, policy.get("worker_depth", 0)) if as_worker else {}

    def mcp_group(entry, server_key):
        """What an `mcp__*` / `mcp__<server>__*` grant reaches right now."""
        reachable, unavailable = {}, {}
        for qualified, (server, _status) in mcp.items():
            if server_key != "*" and server != server_key:
                continue
            if qualified in expand_disabled or qualified in owner_denied:
                continue
            problem = mcp_problem(qualified)
            if problem:
                unavailable.setdefault(server, problem)
            else:
                reachable.setdefault(server, set()).add(qualified)
        tools = set().union(*reachable.values()) if reachable else set()
        return {
            "entry": entry,
            "servers": sorted(reachable),
            "server_count": len(reachable),
            "tool_count": len(tools),
            "deferred_count": len(tools & deferred_names),
            "unavailable_servers": dict(sorted(unavailable.items())),
        }

    denied, effective, conditional, groups = [], [], [], []
    wildcard_known, wildcard_connected = set(), set()
    for tool in wanted:
        if tool in selected and tool in expand_disabled and profile.get("tool_access") == "selected":
            denied.append({"tool": tool, "reason": "profile_disabled", "detail": (
                "listed in this loadout's enabled_tools AND its disabled_tools; the denial wins")})
            continue
        server_key = mcp_wildcard_server(tool)
        if server_key is not None:
            # A grant by shape (manage_agent_loadout: "mcp__<server>__* for a
            # whole server, mcp__* for all of them"), not a tool name, so it is
            # never "unknown": report what it expands to instead. 2026-09-27:
            # Odysseus Admin read DEGRADED only because `mcp__*` was looked up
            # as if it were a tool called "mcp__*".
            group = mcp_group(tool, server_key)
            wildcard_known.add(tool)
            if group["tool_count"]:
                wildcard_connected.add(tool)
            # `selected` for an "all" profile is `wanted & authorized`, and the
            # caller's authorized set holds tool names, never a wildcard — so
            # for those profiles the grant is judged the way the clamp judges it.
            granted = tool in selected or (
                profile.get("tool_access") not in {"selected", "none"}
                and _caller_may_grant_mcp(tool, policy))
            if not granted:
                reason, detail = (
                    ("not_requested", "not in this profile's enabled_tools")
                    if _caller_may_grant_mcp(tool, policy)
                    else ("parent_policy", "the calling chat may not use it, so it cannot grant it"))
                denied.append({"tool": tool, "reason": reason, "detail": detail})
            elif server_key == "*" or group["tool_count"]:
                # "Every connected server" asks for whatever is connected, so
                # none connected right now is information, not a broken grant.
                effective.append(tool)
                groups.append(group)
            else:
                denied.append({"tool": tool, "reason": "mcp_server", "detail": (
                    group["unavailable_servers"].get(server_key)
                    or f"MCP server {server_key} offers no tools right now (not connected, or not configured)")})
            continue
        if tool in selected and tool in withheld:
            denied.append({"tool": tool, "reason": "worker_policy",
                           "detail": f"a worker started from this chat does not get it: {withheld[tool]}"})
            continue
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
        "known": [t for t in wanted if t in known or t in wildcard_known],
        "connected": [t for t in wanted if (t in known and (t not in mcp or mcp[t][1] == "connected"))
                      or t in wildcard_connected],
        "authorized": [t for t in wanted if t in authorized
                       or (t in wildcard_known and _caller_may_grant_mcp(t, policy))],
        "selected_for_profile": sorted(effective),
        "deferred_schema": sorted(set(effective) & deferred_names),
        # One row per MCP wildcard grant: what `mcp__*` / `mcp__<server>__*`
        # reaches right now, since the tools themselves cannot be listed.
        "mcp_groups": groups,
        "conditional": conditional,
        "denied": denied,
        "mission_critical_missing": missing,
    }


def mcp_wildcard_server(entry: str) -> Optional[str]:
    """``"*"`` for ``mcp__*``, the server id for ``mcp__<server>__*``, else None."""
    from src.tool_policy import ALL_MCP_WILDCARD, split_mcp_tool_name

    if entry == ALL_MCP_WILDCARD:
        return "*"
    parts = split_mcp_tool_name(entry)
    if parts and parts[1] == "*":
        return parts[0]
    return None


def _clamp_tools(prof: Dict[str, Any], policy: Dict[str, Any], notes: List[str]) -> None:
    from src.tool_policy import denied_by_allowlist, is_mcp_tool_name

    known: Set[str] = policy["known_tools"]
    explicit_denied = agent_profiles.expand_tool_aliases(prof.get("disabled_tools") or [])
    if prof["tool_access"] == "none":
        wanted: Set[str] = set()
    elif prof["tool_access"] == "selected":
        listed = set(prof.get("enabled_tools") or [])
        wanted = listed - explicit_denied
        conflicting = sorted(listed & explicit_denied)
        if conflicting:
            # The denial wins, as it does at execution. It used to win
            # silently, so a loadout "repaired" by adding manage_git saved
            # without it and reported success (2026-09-26).
            notes.append(
                f"tools: {', '.join(conflicting[:8])}{'…' if len(conflicting) > 8 else ''} listed in both "
                "enabled_tools and disabled_tools, so not granted; remove them from disabled_tools "
                "(or name disabled_tools in 'clear') to grant them"
            )
    else:
        wanted = (known | _caller_mcp_grants(policy)) - explicit_denied

    def _mcp_shaped(name: str) -> bool:
        return is_mcp_tool_name(name) or (name.startswith("mcp__") and name.endswith("*"))

    # Native names go through the caller's own allowed set (alias-expanded, as
    # `expand_tool_aliases` has always done here). MCP entries cannot: they are
    # grants by shape, so each is checked against the caller's allowlist AND its
    # connection list by `_caller_may_grant_mcp`.
    native_wanted = {name for name in wanted if not _mcp_shaped(name)}
    mcp_wanted = wanted - native_wanted
    granted = (native_wanted & agent_profiles.expand_tool_aliases(policy["allowed_tools"])) | {
        name for name in mcp_wanted if _caller_may_grant_mcp(name, policy)
    }
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
    # Belt-and-braces only, and built from the shared inversion rather than a
    # second hand-written subtraction: `launch_worker()` hands the profile's own
    # `disabled_tools` straight to `run_headless()`. The allowlist itself is
    # what binds, through the worker chat's stored `tool_access`/`enabled_tools`
    # (`agent_profiles.session_patch`), so this list going stale can no longer
    # widen anything -- it can only deny.
    complement = sorted(
        denied_by_allowlist(
            known,
            tool_access="selected",
            enabled_tools=agent_profiles.expand_tool_aliases(granted),
            disabled_tools=prof.get("disabled_tools") or [],
        )
    )
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


def _align_mcp_with_tool_allowlist(prof: Dict[str, Any], notes: List[str]) -> None:
    """Make the stored connection list say what the tool allowlist will permit.

    The tool allowlist is the gate that decides; `allowed_mcp_servers` is what
    the prompt advertises. A loadout narrowed to named tools that still listed
    every connected server would advertise servers whose every tool the gate
    rejects — the phantom-tool failure `website/design-patterns.md` names under
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
