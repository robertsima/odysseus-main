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

_ACTIONS = ("list", "get", "capabilities", "create", "update", "delete", "start", "status")
# Fields an agent may set. `name` is required; everything else falls back to
# agent_profiles' own defaults.
_FIELDS = (
    "name", "description", "instructions", "model", "model_fallbacks", "model_access",
    "allowed_models", "tool_access", "enabled_tools", "disabled_tools", "memory_access",
    "skill_access", "skill_names", "mcp_access", "allowed_mcp_servers",
    "private_vault_access", "approval_mode", "delegation_policy",
    "max_parallel_workers", "max_rounds",
)


# Numeric fields whose valid range starts at 1, so a supplied 0 is a provider
# filling in a blank rather than a setting. `max_parallel_workers` is pointedly
# NOT here: 0 is a real value there and means "this worker may start no
# children of its own".
_ZERO_MEANS_UNSET = frozenset({"max_rounds"})


def _is_blank(key: str, value: Any) -> bool:
    """Whether a supplied field carries no instruction from the caller.

    Native function-calling providers fill every property they were shown, so a
    real call arrives as `{"action": "update", "name": "X", "instructions": "",
    "model": "", "tool_access": "", "max_rounds": 0, ...}` — one intended edit
    and a dozen blanks. Treating those blanks as values is what made an update
    destructive. `False` is a real setting and is never blank.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return not value
    if key in _ZERO_MEANS_UNSET and isinstance(value, (int, float)) and not isinstance(value, bool):
        return value <= 0
    return False


def _tool_examples(policy: Dict[str, Any], limit: int = 14) -> str:
    names = sorted(policy["allowed_tools"])
    if not names:
        return "no tools at all (this chat's own tool access is empty)"
    shown = ", ".join(names[:limit])
    return f"{len(names)} tool(s), e.g. {shown}" if len(names) > limit else shown


def _requested(args: Dict[str, Any], base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The loadout definition, given either inline or under `loadout`.

    With `base` (an update), the result is that stored profile with only the
    fields the caller actually supplied overridden. Without it (a create), it is
    just the supplied fields, and `validate_profiles` fills the rest with
    defaults.

    Blanks are ignored rather than written, so an untouched field keeps its
    stored value instead of resetting to a default — for `tool_access` that
    default is "all", which turned "rename this loadout" into "re-grant it every
    tool its author can use". A field is unset deliberately by naming it in
    `clear`, never by sending it empty.
    """
    nested = args.get("loadout")
    source = nested if isinstance(nested, dict) else args
    supplied = {key: source[key] for key in _FIELDS
                if key in source and not _is_blank(key, source[key])}
    if base is None:
        return supplied
    merged = {key: base[key] for key in _FIELDS if key in base}
    merged.update(supplied)
    raw_clear = args.get("clear")
    for key in raw_clear if isinstance(raw_clear, (list, tuple)) else []:
        if key in _FIELDS and key != "name":
            merged.pop(str(key), None)
    return merged


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
        rows = [agent_loadouts.discovery_summary(p) for p in agent_profiles.load_profiles()]
        return {
            "response": (
                f"{len(rows)} loadout(s). Use action='get' with a name to inspect its full policy "
                "and instructions."
            ),
            "loadouts": rows,
            "exit_code": 0,
        }

    if action == "status":
        # What happened to the workers this chat started. Without it the only
        # way to find out was to grep the activity JSONL by hand -- which is
        # exactly what the 2026-09-17 transcript spent twenty rounds doing,
        # while the answer sat in the run registry the whole time.
        from src import agent_activity

        rows = []
        for run in agent_activity.list_runs(session_id=session_id, limit=int(args.get("limit") or 20)):
            summary = run.get("summary") or {}
            row = {
                "run_id": run["run_id"], "status": run["status"], "title": run["title"],
                "worker_session": summary.get("target_session") or run.get("session_id"),
                "loadout": summary.get("profile"), "model": summary.get("model"),
                "started_at": run.get("started_at"), "finished_at": run.get("finished_at"),
                "tool_calls": summary.get("steps"),
                "max_rounds": summary.get("max_rounds"),
                "ran_out_of_rounds": bool(summary.get("rounds_exhausted")),
                "result_excerpt": summary.get("result_excerpt"),
                "error": summary.get("error"),
            }
            rows.append({key: value for key, value in row.items() if value not in (None, "")})
        running = sum(1 for row in rows if row.get("status") == "running")
        cut_off = [row["run_id"] for row in rows if row.get("ran_out_of_rounds")]
        return {
            "response": (
                f"{len(rows)} worker run(s) for this chat; {running} still running."
                + (f" Cut off by their round budget: {', '.join(cut_off)} — these did NOT finish their task; "
                   "restart them with a larger max_rounds rather than reporting their partial work as done."
                   if cut_off else "")
                + " A result_excerpt is the worker's own claim, not verified work."
            ),
            "runs": rows,
            "running": running,
            "exit_code": 0,
        }

    if action == "capabilities":
        # What a loadout authored from this chat may contain at most. Without
        # it the agent has to discover the clamp by tripping over it.
        detail = bool(args.get("detail"))
        ceiling = {
            # The tool schema already tells an agent callable names. Repeating
            # every permitted name here costs a large tool result before it has
            # picked a loadout; request detail=true only when authoring an
            # explicit selected-tools policy.
            "tool_count": len(policy["allowed_tools"]),
            "tool_examples": sorted(policy["allowed_tools"])[:12],
            "memory_access": policy["memory_access"],
            "skill_access": policy["skill_access"],
            "skills": sorted(policy["skill_names"]),
            "model_access": policy["model_access"],
            "allowed_models": sorted(policy["allowed_models"]),
            "allowed_mcp_servers": list(policy["allowed_mcp_servers"]),
            "private_vault_access": policy["private_vault_access"],
            "delegation_policy": policy["delegation_policy"],
            "max_parallel_workers": policy["max_parallel_workers"],
            "worker_limit_scope": "parent_chat (separate from provider-wide concurrent jobs)",
            "approval_mode_floor": policy["approval_mode"],
        }
        if detail:
            ceiling["tools"] = sorted(policy["allowed_tools"])
        return {
            "response": "Ceiling for loadouts created from this chat",
            "ceiling": ceiling,
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
        base = None
        if action == "update":
            base = agent_profiles.get_profile(name)
            if base is None:
                return {"error": f"no loadout named {name!r}; use action='create'", "exit_code": 1}
        requested = _requested(args, base)
        if not requested.get("name"):
            return {"error": "name is required", "exit_code": 1}
        # Keep the stored name's capitalisation rather than whatever the caller
        # typed, so `update` never renames a loadout as a side effect.
        if base is not None:
            requested["name"] = base["name"]
        try:
            profile, narrowed = agent_loadouts.clamp(requested, policy)
        except ValueError as exc:
            return {"error": f"manage_agent_loadout: {exc}", "exit_code": 1}
        if agent_loadouts.tool_starved(narrowed):
            # Storing it would only defer the failure to every worker it ever
            # starts. Refuse here, where the author can still fix it.
            return {
                "error": (
                    f"{action}: none of the tools requested for {requested['name']!r} are available to this "
                    "chat, so the loadout would start workers with no tools at all. "
                    + "; ".join(narrowed)
                    + f". This chat can grant: {_tool_examples(policy)}. "
                    "Pick from those, or ask the user to widen this chat's own tool access."
                ),
                "narrowed": narrowed,
                "available_tool_count": len(policy["allowed_tools"]),
                "exit_code": 1,
            }
        try:
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
        return {
            "error": (
                f"Worker capacity reached: {running} active of this chat's limit {limit}. "
                "This is the parent chat's Child workers limit, separate from provider-wide concurrent jobs. "
                "Do not retry a start while capacity is unchanged."
            ),
            "blocked": True,
            "blocked_reason": "worker_capacity",
            "capacity_scope": "parent_chat",
            "configuration_hint": "Agents > select the parent chat > Loadout > Child workers. Only the user may raise this ceiling.",
            "capacity": {"limit": limit, "active": running, "available": max(0, limit - running)},
            "exit_code": 1,
        }
    started_profile = agent_profiles.get_profile(name) if name else None
    if name and started_profile is None:
        rows = agent_profiles.load_profiles()
        available = ", ".join(
            f"{p['name']} ({len(p['enabled_tools'])} tools)" if p["tool_access"] == "selected"
            else f"{p['name']} ({p['tool_access']} tools)"
            for p in rows
        ) or "none are defined"
        return {
            "error": (
                f"no loadout named {name!r}. Available: {available}. "
                "Pick one whose tools actually fit this task, or create one first — "
                "a near-miss name is not a near-miss loadout."
            ),
            "exit_code": 1,
        }
    # A worker that cannot read, search or run anything will spend its whole
    # round budget explaining that. Refuse at launch rather than produce one.
    unusable = agent_loadouts.unusable_reason(started_profile) if started_profile else None
    if unusable:
        usable = [p["name"] for p in agent_profiles.load_profiles()
                  if agent_loadouts.unusable_reason(p) is None]
        return {
            "error": (
                f"start: {unusable}. Fix it with action='update' (set tool_access and enabled_tools "
                "from this chat's own tools), or start one of: "
                + (", ".join(usable) if usable else "none — every stored loadout has this problem")
            ),
            "blocked": True,
            "blocked_reason": "loadout_has_no_tools",
            "loadout": agent_loadouts.summarize(started_profile),
            "exit_code": 1,
        }

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
    # What it is actually going to run with. The model has to be able to see a
    # wrong-fit loadout without waiting for the worker to report that it could
    # not do the job, and the round budget is the number an "it ran out of
    # rounds" result has to be read against.
    preflight = {
        "loadout": started_profile["name"] if started_profile else "ad-hoc worker",
        "model": result.get("model") or "inherit",
        "max_rounds": result.get("max_rounds"),
        "tools": (started_profile["enabled_tools"] if started_profile
                  and started_profile["tool_access"] == "selected" else
                  (started_profile["tool_access"] if started_profile else "all")),
        "skills": started_profile["skill_names"] if started_profile else [],
        "allowed_mcp_servers": started_profile["allowed_mcp_servers"] if started_profile else [],
    }
    logger.info("[agent-loadout] start loadout=%s run=%s child=%s model=%s rounds=%s tools=%s",
                preflight["loadout"], result.get("run_id"), result.get("session_id"),
                preflight["model"], preflight["max_rounds"],
                preflight["tools"] if isinstance(preflight["tools"], str) else ",".join(preflight["tools"]))
    tool_note = (preflight["tools"] if isinstance(preflight["tools"], str)
                 else ", ".join(preflight["tools"]) or "none")
    return {
        "response": (
            f"Started {name or 'worker'} in chat {result.get('session_name')} on {preflight['model']} "
            f"with a {preflight['max_rounds']}-round budget and these tools: {tool_note}. "
            "It runs detached; its progress appears on this chat's activity feed. "
            "If those tools cannot do the task you just described, stop it and fix the loadout "
            "instead of waiting for the result."
        ),
        "preflight": preflight,
        **result,
        "exit_code": 0,
    }


class ManageAgentLoadoutTool:
    async def execute(self, content: str, ctx: dict) -> Dict[str, Any]:
        return await manage_agent_loadout(content, ctx.get("session_id"), owner=ctx.get("owner"))
