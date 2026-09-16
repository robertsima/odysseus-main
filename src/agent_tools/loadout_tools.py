"""``manage_agent_loadout`` — let an agent define and start worker loadouts.

Creating a loadout is a policy action, so the interesting part is not the CRUD:
it is :mod:`src.agent_loadouts`, which intersects whatever the agent asks for
with the calling chat's own policy before anything is stored. The tool's job is
to parse arguments, refuse actions the caller's delegation policy forbids, and
report back exactly which parts of a request were narrowed.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src import agent_loadouts, agent_profiles
from src.tool_utils import _parse_tool_args

logger = logging.getLogger(__name__)

_ACTIONS = ("list", "get", "capabilities", "create", "update", "delete", "start")
# Fields an agent may set. `name` is required; everything else falls back to
# agent_profiles' own defaults.
_FIELDS = (
    "name", "description", "instructions", "model", "model_fallbacks", "model_access",
    "allowed_models", "tool_access", "enabled_tools", "disabled_tools", "memory_access",
    "skill_access", "skill_names", "mcp_access", "allowed_mcp_servers",
    "private_vault_access", "approval_mode", "delegation_policy",
    "max_parallel_workers", "max_rounds",
)


def _requested(args: Dict[str, Any]) -> Dict[str, Any]:
    """The loadout definition, given either inline or under `loadout`."""
    nested = args.get("loadout")
    source = nested if isinstance(nested, dict) else args
    return {key: source[key] for key in _FIELDS if key in source}


async def manage_agent_loadout(content: str, session_id: Optional[str] = None,
                               owner: Optional[str] = None) -> Dict[str, Any]:
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "manage_agent_loadout: JSON object required", "exit_code": 1}
    if not isinstance(args, dict):
        return {"error": "manage_agent_loadout: JSON object required", "exit_code": 1}

    action = str(args.get("action") or "list").strip().lower()
    if action not in _ACTIONS:
        return {"error": f"action must be one of {', '.join(_ACTIONS)}", "exit_code": 1}

    policy = agent_loadouts.caller_policy(session_id, owner)

    if action == "list":
        rows = [agent_loadouts.summarize(p) for p in agent_profiles.load_profiles()]
        return {"response": f"{len(rows)} loadout(s)", "loadouts": rows, "exit_code": 0}

    if action == "capabilities":
        # What a loadout authored from this chat may contain at most. Without
        # it the agent has to discover the clamp by tripping over it.
        return {
            "response": "Ceiling for loadouts created from this chat",
            "ceiling": {
                "tools": sorted(policy["allowed_tools"]),
                "memory_access": policy["memory_access"],
                "skill_access": policy["skill_access"],
                "skills": sorted(policy["skill_names"]),
                "model_access": policy["model_access"],
                "allowed_models": sorted(policy["allowed_models"]),
                "allowed_mcp_servers": list(policy["allowed_mcp_servers"]),
                "private_vault_access": policy["private_vault_access"],
                "delegation_policy": policy["delegation_policy"],
                "max_parallel_workers": policy["max_parallel_workers"],
                "approval_mode_floor": policy["approval_mode"],
            },
            "exit_code": 0,
        }

    name = str(args.get("name") or (args.get("loadout") or {}).get("name") or "").strip()

    if action == "get":
        profile = agent_profiles.get_profile(name)
        if profile is None:
            return {"error": f"no loadout named {name!r}", "exit_code": 1}
        return {"response": f"Loadout {profile['name']}", "loadout": agent_loadouts.summarize(profile),
                "instructions": profile["instructions"], "exit_code": 0}

    if action == "delete":
        if not agent_loadouts.delete(name):
            return {"error": f"no loadout named {name!r}", "exit_code": 1}
        return {"response": f"Deleted loadout {name!r}", "exit_code": 0}

    if action in ("create", "update"):
        requested = _requested(args)
        if not requested.get("name"):
            return {"error": "name is required", "exit_code": 1}
        try:
            profile, narrowed = agent_loadouts.clamp(requested, policy)
            saved = agent_loadouts.save(profile, replace=(action == "update"))
        except ValueError as exc:
            return {"error": f"manage_agent_loadout: {exc}", "exit_code": 1}
        response = f"{'Updated' if action == 'update' else 'Created'} loadout {saved['name']!r}"
        if narrowed:
            response += f"; narrowed to this chat's own policy in {len(narrowed)} place(s)"
        return {"response": response, "loadout": agent_loadouts.summarize(saved),
                "narrowed": narrowed, "exit_code": 0}

    # action == "start"
    task = str(args.get("task") or "").strip()
    if not task:
        return {"error": "start: describe the whole task — the worker begins with no other context",
                "exit_code": 1}
    if policy["delegation_policy"] == "never":
        return {"error": "start: this chat's delegation policy is 'never'", "exit_code": 1}
    from src import agent_control

    # Same limit the other spawning tools are held to (the gate in
    # tool_execution). Checked here because that gate keys on the tool name,
    # and this tool's read-only actions must not be blocked by a busy chat.
    limit = policy["max_parallel_workers"]
    running = agent_control.live_children(session_id)
    if limit <= 0 or running >= limit:
        return {"error": f"start: this chat's worker limit is {limit}; {running} already running",
                "exit_code": 1}
    if name and agent_profiles.get_profile(name) is None:
        available = ", ".join(p["name"] for p in agent_profiles.load_profiles()) or "none are defined"
        return {"error": f"no loadout named {name!r}. Available: {available}", "exit_code": 1}

    # Report to the calling chat unless the agent explicitly asks for a
    # standalone worker (parent_session: ""), so a worker is not orphaned by
    # default.
    parent = args.get("parent_session", session_id)
    parent = str(parent).strip() if parent else None
    if parent and parent != session_id:
        # /api/agents/launch owner-checks its parent chat; this path has to do
        # the same, or an agent could name someone else's chat and have the
        # worker's model copied from it and its progress published into it.
        # Exact owner match, as in send_to_session: a null-owner session is not
        # an authenticated caller's either.
        error = {"error": f"parent_session {parent!r} not found", "exit_code": 1}
        try:
            from src.ai_interaction import get_session_manager

            manager = get_session_manager()
            target = manager.get_session(parent) if manager else None
        except Exception:
            return error
        if target is None or (owner and getattr(target, "owner", None) != owner):
            return error
    try:
        result = await agent_control.launch_worker(
            owner=owner, task=task, profile_name=name or None,
            parent_session=parent, model=str(args.get("model") or "").strip() or None,
        )
    except (ValueError, RuntimeError) as exc:
        return {"error": f"start: {exc}", "exit_code": 1}
    return {
        "response": (
            f"Started {name or 'worker'} in chat {result.get('session_name')}. It runs detached; "
            "its progress appears on this chat's activity feed."
        ),
        **result,
        "exit_code": 0,
    }


class ManageAgentLoadoutTool:
    async def execute(self, content: str, ctx: dict) -> Dict[str, Any]:
        return await manage_agent_loadout(content, ctx.get("session_id"), owner=ctx.get("owner"))
