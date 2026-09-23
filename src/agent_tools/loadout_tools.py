"""``manage_agent_loadout`` — let an agent define and start worker loadouts.

Creating a loadout is a policy action, so the interesting part is not the CRUD:
it is :mod:`src.agent_loadouts`, which intersects whatever the agent asks for
with the calling chat's own policy before anything is stored. The tool's job is
to parse arguments, refuse actions the caller's delegation policy forbids, and
report back exactly which parts of a request were narrowed.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from src import agent_loadouts, agent_profiles
from src.worker_preflight import WorkerBlocked
from src.tool_utils import _parse_tool_args

logger = logging.getLogger(__name__)

_ACTIONS = ("list", "get", "capabilities", "preflight", "create", "update", "delete", "start", "status", "stop")
# Fields an agent may set. `name` is required; everything else falls back to
# agent_profiles' own defaults.
_FIELDS = (
    "name", "description", "instructions", "persona_name", "temperature", "max_tokens", "reasoning_effort",
    "model", "model_fallbacks", "model_access",
    "allowed_models", "tool_access", "enabled_tools", "disabled_tools", "memory_access",
    "skill_access", "skill_names", "mcp_access", "allowed_mcp_servers",
    "private_vault_access", "approval_mode", "delegation_policy",
    "max_parallel_workers", "max_rounds",
)


# Numeric fields whose valid range starts at 1, so a supplied 0 is a provider
# filling in a blank rather than a setting. `max_parallel_workers` is pointedly
# NOT here: 0 is a real value there and means "this worker may start no
# children of its own".
# `temperature`/`max_tokens` join them: a filled-in 0 would pin a loadout to
# greedy sampling, and max_tokens 0 already means "server decides".
_ZERO_MEANS_UNSET = frozenset({"max_rounds", "temperature", "max_tokens"})


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


# How many tool names a log line or a tool result spells out before switching
# to a count. A worker granted 200 tools produced a 200-name wall in the server
# log and again in the model's context (2026-09-17); the names past the first
# dozen tell nobody anything.
_TOOL_LIST_PREVIEW = 12


def _tool_list_note(tools: Any) -> str:
    if isinstance(tools, str):
        return tools
    names = [str(t) for t in (tools or [])]
    if not names:
        return "none"
    shown = ", ".join(names[:_TOOL_LIST_PREVIEW])
    if len(names) <= _TOOL_LIST_PREVIEW:
        return shown
    return f"{shown} … (+{len(names) - _TOOL_LIST_PREVIEW} more, {len(names)} total)"


# Shorthand an agent may put in `enabled_tools`: the read-only tools this chat
# can grant, resolved at authoring time. It is also what a loadout gets when
# its author names no tool policy at all.
READ_ONLY_TOKEN = "@read_only"


def _scope_tools(requested: Dict[str, Any], policy: Dict[str, Any], *, action: str,
                 supplied_access: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Settle an agent-authored tool policy before the clamp sees it.

    Returns ``(error, notes)``. The clamp only narrows to what the caller has,
    which for ``tool_access: "all"`` is *everything* the caller has: two
    read-only audit loadouts were stored on 2026-09-17 with ~200 tools each —
    bash, python, send_email, vault_unlock, manage_settings, Bluesky posting,
    the whole browser MCP surface — because the model wrote "all" and nothing
    asked it what the task needed. So:

    - "all" is refused, with the read-only set and the mutating tools it would
      have granted, so the author names what the worker needs;
    - no tool policy at all defaults to the read-only set the chat can grant
      (a note says so), falling back to the caller's own tools only when the
      chat has no read-only tools to give — a loadout that grants nothing is
      the failure the starved-loadout check exists for, not an outcome;
    - ``@read_only`` inside ``enabled_tools`` expands to that same set.
    """
    allowed = set(policy["allowed_tools"])
    read_only = sorted(allowed & agent_loadouts.read_only_tools())
    notes: List[str] = []
    if supplied_access == "all":
        mutating = sorted(allowed - set(read_only))
        return {
            "error": (
                f"{action}: tool_access 'all' would hand this worker every one of the "
                f"{len(allowed)} tools this chat has, including {len(mutating)} that write, send, "
                f"post or run things ({_tool_list_note(mutating[:8]) if mutating else 'none'}). "
                "Name what the task needs instead: tool_access='selected' with enabled_tools "
                f"listing exact names, or enabled_tools=[\"{READ_ONLY_TOKEN}\"] for the "
                f"{len(read_only)} read-only tools ({_tool_list_note(read_only)}) plus any others "
                "by name. action='capabilities' with detail=true lists every grantable name."
            ),
            "read_only_tools": read_only,
            "mutating_tool_count": len(mutating),
            "exit_code": 1,
        }, notes
    enabled = requested.get("enabled_tools")
    if isinstance(enabled, list) and READ_ONLY_TOKEN in enabled:
        requested["enabled_tools"] = sorted(
            {str(t) for t in enabled if t != READ_ONLY_TOKEN} | set(read_only))
        requested.setdefault("tool_access", "selected")
        notes.append(f"tools: {READ_ONLY_TOKEN} expanded to {len(read_only)} read-only tool(s)")
    if not requested.get("tool_access"):
        if read_only:
            requested["tool_access"] = "selected"
            requested["enabled_tools"] = sorted(set(requested.get("enabled_tools") or []) | set(read_only))
            notes.append(
                f"tools: no tool policy given, so this loadout gets the {len(read_only)} read-only "
                f"tool(s) this chat can grant ({_tool_list_note(read_only)}); name enabled_tools "
                "to widen it"
            )
        else:
            notes.append("tools: no tool policy given and this chat has no read-only tools, "
                         "so the loadout gets the chat's own tools")
    return None, notes


def _model_problem(spec: str, owner: Optional[str]) -> Optional[str]:
    """Why ``spec`` cannot be started with right now, or None.

    Checked when a loadout is written, not when it is started: `model:
    "gpt-luna-5.6"` (a misspelling of gpt-5.6-luna) was accepted at create and
    only failed at start with "has no available model", two rounds later.
    A registry that cannot be read is not the author's mistake, so that is
    not a problem here.
    """
    try:
        from src.ai_interaction import _resolve_model

        _resolve_model(spec, owner=owner)
    except ValueError as exc:
        return str(exc)
    except Exception:  # registry unavailable: leave it to start
        logger.debug("loadout: model check for %r skipped", spec, exc_info=True)
    return None


def _available_model_ids(owner: Optional[str], limit: int = 40) -> List[str]:
    """Model ids the author could have meant, from every enabled endpoint."""
    ids: List[str] = []
    try:
        from src.auth_helpers import owner_filter
        from src.database import ModelEndpoint, SessionLocal
        from src.endpoint_resolver import build_headers, resolve_endpoint_runtime
        from src.llm_core import list_model_ids

        db = SessionLocal()
        try:
            query = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
            if owner:
                query = owner_filter(query, ModelEndpoint, owner)
            endpoints = list(query.all())
        finally:
            db.close()
        for ep in endpoints:
            try:
                base, api_key = resolve_endpoint_runtime(ep, owner=owner)
                ids.extend(list_model_ids(base, timeout=5, headers=build_headers(api_key, base),
                                          owner=owner, endpoint_id=getattr(ep, "id", None)))
            except Exception:
                continue
    except Exception:
        logger.debug("loadout: could not list model ids", exc_info=True)
    seen: List[str] = []
    for model_id in ids:
        if model_id and model_id not in seen:
            seen.append(model_id)
    return seen[:limit]


def _supplied(args: Dict[str, Any]) -> Dict[str, Any]:
    """The loadout fields this call actually carries, given inline or under `loadout`."""
    nested = args.get("loadout")
    source = nested if isinstance(nested, dict) else args
    return {key: source[key] for key in _FIELDS
            if key in source and not _is_blank(key, source[key])}


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
    supplied = _supplied(args)
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

    if action == "stop":
        # Actually stop a worker this chat started. Without it, "stop the
        # scout" reached the worker only as a message (message_agent), which
        # it read and ignored for ten more rounds until the user cancelled it
        # by hand. Same path as the Agents panel's Stop button.
        from src import agent_activity
        from src import agent_control

        run_id = str(args.get("run_id") or "").strip()
        worker = str(args.get("worker_session") or args.get("session_id") or "").strip()
        mine = agent_activity.list_runs(session_id=session_id, limit=100)
        candidates = [
            run for run in mine
            if run.get("status") == "running"
            and (not run_id or run.get("run_id") == run_id)
            and (not worker or (run.get("summary") or {}).get("target_session") == worker
                 or run.get("session_id") == worker)
        ]
        if not run_id and not worker:
            if len(candidates) != 1:
                running = [{"run_id": r["run_id"], "title": r.get("title")} for r in candidates]
                return {"error": "stop needs run_id (see action=status); "
                                 f"{len(running)} worker(s) running: {running}", "exit_code": 1}
        if not candidates:
            return {"error": "No running worker started by this chat matches; see action=status.", "exit_code": 1}
        stopped = []
        for run in candidates:
            try:
                result = await agent_control.stop_run(run["run_id"])
            except (LookupError, ValueError) as exc:
                result = {"stopped": False, "reason": str(exc)}
            stopped.append({"run_id": run["run_id"], "title": run.get("title"), **result})
        ok = any(item.get("stopped") for item in stopped)
        return {"response": ("Stopped: " if ok else "Not stopped: ") + ", ".join(
                    f"{item['run_id']} ({item.get('how') or item.get('reason') or item.get('status')})" for item in stopped),
                "runs": stopped, "exit_code": 0 if ok else 1}

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

    if action == "preflight":
        profile = agent_profiles.get_profile(name)
        if profile is None:
            return {"error": f"no loadout named {name!r}", "exit_code": 1}
        from src.profile_readiness import profile_readiness, render

        readiness = profile_readiness(profile, policy, owner,
                                      required_tools=args.get("required_tools") or [])
        return {"response": f"Loadout {profile['name']}: {render(readiness)}",
                "readiness": readiness, "exit_code": 0}

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
        supplied = _supplied(args)
        error, scope_notes = _scope_tools(
            requested, policy, action=action,
            supplied_access=str(supplied.get("tool_access") or "").strip().lower())
        if error:
            return error
        # Only the models named in THIS call are checked, so an endpoint that is
        # down does not block renaming a loadout that already has a model.
        if policy["model_access"] != "current":
            for spec in [supplied.get("model"), *(supplied.get("model_fallbacks") or [])]:
                problem = _model_problem(str(spec), owner) if spec else None
                if problem:
                    available = _available_model_ids(owner)
                    return {
                        "error": (
                            f"{action}: model {spec!r} is not available: {problem}. "
                            + (f"Available model ids: {', '.join(available)}. "
                               if available else "")
                            + "Use one of those exactly, or omit model to inherit the calling chat's."
                        ),
                        "available_models": available,
                        "exit_code": 1,
                    }
        requested_tools = list(requested.get("enabled_tools") or [])
        required_tools = args.get("required_tools") or (args.get("loadout") or {}).get("required_tools") or []
        if not isinstance(required_tools, list):
            return {"error": "required_tools must be a list of exact tool names", "exit_code": 1}
        if required_tools and requested.get("tool_access") == "selected":
            requested["enabled_tools"] = sorted(set(requested_tools) | {str(t) for t in required_tools})
            requested_tools = requested["enabled_tools"]
        try:
            profile, narrowed = agent_loadouts.clamp(requested, policy)
        except ValueError as exc:
            return {"error": f"manage_agent_loadout: {exc}", "exit_code": 1}
        narrowed = [*scope_notes, *narrowed]
        matrix = agent_loadouts.capability_matrix(requested_tools, required_tools, profile, policy, owner)
        if matrix["mission_critical_missing"]:
            # A profile that saves without what its mission needs is the
            # failure this refuses: it only moves the error to every worker
            # it would ever start.
            return {
                "error": (
                    f"{action}: {requested['name']!r} was not saved; required tool(s) unavailable: "
                    + "; ".join(f"{row['tool']} ({row['reason']}: {row['detail']})"
                                for row in matrix["denied"] if row["tool"] in matrix["mission_critical_missing"])
                    + ". Fix the cause (reconnect the server, widen this chat's policy) or drop the "
                    "requirement."
                ),
                "capabilities": matrix,
                "narrowed": narrowed,
                "exit_code": 1,
            }
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
        try:
            from src.profile_readiness import profile_readiness, render

            readiness = profile_readiness(saved, policy, owner, required_tools=required_tools)
            response += ". Readiness " + render(readiness)
        except Exception as exc:
            logger.warning("loadout: readiness check failed for %s", saved["name"], exc_info=True)
            readiness = {"status": "UNKNOWN", "error": f"{type(exc).__name__}"}
            response += f". Capability status: {matrix['status']} (readiness check failed)"
        readback = agent_profiles.get_profile(saved["name"])
        consistent = bool(readback) and (
            set(readback.get("enabled_tools") or []) == set(saved.get("enabled_tools") or [])
            and readback.get("tool_access") == saved.get("tool_access")
            and set(readback.get("skill_names") or []) == set(saved.get("skill_names") or []))
        if not consistent:
            response += ". WARNING: the stored loadout read back with different bindings"
        return {"response": response, "loadout": agent_loadouts.summarize(saved),
                "capabilities": matrix, "readiness": readiness, "readback_consistent": consistent,
                "narrowed": narrowed, "exit_code": 0}

    # action == "start"
    task = str(args.get("task") or "").strip()
    if not task:
        # Spell out the call shape: two rounds were spent on
        # {"action":"start","name":...,"detail":true} before the model found
        # that `task` is the whole assignment and `detail` is not a start field.
        return {
            "error": (
                "start: 'task' is required and was not given. Send "
                '{"action": "start", "name": "<loadout>", "task": "<the whole assignment>"} — '
                "the worker begins with no other context. 'detail' is a capabilities parameter, "
                "not a start one."
            ),
            "exit_code": 1,
        }
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
    requires = args.get("requires") or []
    if isinstance(requires, str):
        requires = [requires]
    try:
        result = await agent_control.launch_worker(
            owner=owner, task=task, profile_name=name or None,
            parent_session=parent, model=str(args.get("model") or "").strip() or None,
            workspace=str(args.get("workspace") or "").strip() or None,
            requires=[str(r) for r in requires] if isinstance(requires, list) else [],
        )
    except WorkerBlocked as exc:
        return exc.payload
    except (ValueError, RuntimeError) as exc:
        return {"error": f"start: {exc}", "exit_code": 1}
    # What it is actually going to run with. The model has to be able to see a
    # wrong-fit loadout without waiting for the worker to report that it could
    # not do the job, and the round budget is the number an "it ran out of
    # rounds" result has to be read against.
    granted = (started_profile["enabled_tools"] if started_profile
               and started_profile["tool_access"] == "selected" else
               (started_profile["tool_access"] if started_profile else "all"))
    # The full inventory stays out of both the log and the model's context: a
    # preview and a count say what was granted; `get` has the whole list.
    preflight = {
        "loadout": started_profile["name"] if started_profile else "ad-hoc worker",
        "model": result.get("model") or "inherit",
        "max_rounds": result.get("max_rounds"),
        "tools": granted if isinstance(granted, str) else list(granted[:_TOOL_LIST_PREVIEW]),
        "tool_count": None if isinstance(granted, str) else len(granted),
        "skills": started_profile["skill_names"] if started_profile else [],
        "allowed_mcp_servers": started_profile["allowed_mcp_servers"] if started_profile else [],
        **(result.get("preflight") or {}),
    }
    tool_note = _tool_list_note(granted)
    logger.info("[agent-loadout] start loadout=%s run=%s child=%s model=%s rounds=%s tools=%s",
                preflight["loadout"], result.get("run_id"), result.get("session_id"),
                preflight["model"], preflight["max_rounds"], tool_note)
    return {
        "response": (
            f"Started {name or 'worker'} in chat {result.get('session_name')} on {preflight['model']} "
            f"with these tools: {tool_note}. It runs until the task is done — a round count never "
            "ends it — and it runs detached, so its progress appears on this chat's activity feed "
            "and in action='status'. If those tools cannot do the task you just described, stop it "
            "and fix the loadout instead of waiting for the result."
        ),
        "preflight": preflight,
        **{key: value for key, value in result.items() if key != "preflight"},
        "exit_code": 0,
    }


class ManageAgentLoadoutTool:
    async def execute(self, content: str, ctx: dict) -> Dict[str, Any]:
        return await manage_agent_loadout(content, ctx.get("session_id"), owner=ctx.get("owner"))
