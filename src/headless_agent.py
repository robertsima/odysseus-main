"""Run the agent loop with nobody watching, and keep what it did.

Two callers need the same thing: the background-job monitor continuing a chat
after a detached shell command finishes, and ``send_to_session`` in agent mode
handing a task to another chat's model. Both want the loop's text and the tool
calls it made, in the shape the chat UI already persists on assistant messages
(``metadata.tool_events``), and both want the work to show up on the activity
feed while it runs. This module is that shared drain; the callers only decide
what to do with the result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from src import agent_activity as activity

logger = logging.getLogger(__name__)

# run_id → stop event for every headless run in flight, so the UI can stop one
# sub-agent without stopping the parent turn that is waiting on it. A stopped
# run returns what it produced so far, which re-enters the parent as the tool
# result (the partial work is not thrown away).
_STOP_EVENTS: Dict[str, asyncio.Event] = {}
# run_id -> where the stop came from ("from the Workbench", ...), for the
# worker's hand-back and the log. 2026-09-28: a Planning Command Center worker
# ended "(stopped by the user before finishing)" 23 s after it started, the
# user did not know they had stopped anything, and nothing recorded which of
# the three stop routes had been used.
_STOP_SOURCES: Dict[str, str] = {}
_DEFAULT_STOP_SOURCE = "by the user"
# A wrapper run is the stable public steering target for a headless loop.  The
# loop also creates its own activity telemetry run, which must not replace the
# wrapper's source/summary merely to make steering work.
_STEER_RUNS: Dict[str, Set[str]] = {}


class HeadlessStreamError(RuntimeError):
    """Terminal upstream stream failure that a detached caller must not lose."""

    def __init__(self, message: str, *, status: Optional[int] = None, retryable: bool = False):
        self.status = status
        self.retryable = bool(retryable)
        prefix = f"Upstream model request failed with HTTP {status}" if status else "Upstream model request failed"
        super().__init__(f"{prefix}: {str(message or 'unknown error')[:1000]}")


def _stream_error(payload: Dict[str, Any]) -> HeadlessStreamError:
    raw_status = payload.get("status")
    try:
        status = int(raw_status) if raw_status is not None else None
    except (TypeError, ValueError):
        status = None
    message = payload.get("text") or payload.get("error") or payload.get("message") or "unknown error"
    if isinstance(message, (dict, list)):
        message = json.dumps(message, ensure_ascii=False)
    return HeadlessStreamError(str(message), status=status, retryable=bool(payload.get("retryable")))


def _sse_error_payload(chunk: str) -> Optional[Dict[str, Any]]:
    """Extract a terminal ``event: error`` frame from an SSE chunk."""
    event = ""
    data_lines = []
    for line in str(chunk or "").splitlines():
        if line.startswith("event:"):
            event = line[6:].strip().lower()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if event != "error":
        return None
    raw = "\n".join(data_lines).strip()
    try:
        payload = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        payload = {"error": raw or "unknown stream error"}
    return payload if isinstance(payload, dict) else {"error": str(payload)}


def request_stop(run_id: str, *, by: str = _DEFAULT_STOP_SOURCE) -> bool:
    """Ask a running headless run to stop. Returns False if none is running.

    ``by`` says where the stop came from, phrased to follow "stopped"
    ("from the Workbench"); it is logged and shown in the worker's result.
    """
    event = _STOP_EVENTS.get(run_id)
    if event is None:
        return False
    _STOP_SOURCES[run_id] = str(by or _DEFAULT_STOP_SOURCE)
    logger.info("[agent-stop] run=%s stopped %s", run_id, _STOP_SOURCES[run_id])
    event.set()
    return True


def running_ids() -> Set[str]:
    return set(_STOP_EVENTS)


def steering_run_id(session_id: Optional[str]) -> Optional[str]:
    """Return the sole headless wrapper serving a session, if unambiguous."""
    runs = _STEER_RUNS.get(str(session_id or ""), set())
    return next(iter(runs)) if len(runs) == 1 else None


def serves_run(session_id: Optional[str], run_id: Optional[str]) -> bool:
    """Whether ``run_id`` is a headless wrapper draining ``session_id``'s steers."""
    return bool(run_id) and str(run_id) in _STEER_RUNS.get(str(session_id or ""), set())


def has_steering_runs(session_id: Optional[str]) -> bool:
    """Whether a headless owner exists, including an ambiguous set of owners."""
    return bool(_STEER_RUNS.get(str(session_id or "")))


async def _resolve_context_length(sess) -> int:
    """The model's context window for a headless run, resolved like a chat's.

    A chat turn learns its window in ``routes/chat_helpers.py`` (``maybe_compact``
    asks :func:`src.model_context.get_context_length` for the session's model).
    Headless runs skip that step, and ``core.models.Session`` has no
    ``context_length`` field at all, so the old ``getattr(sess, "context_length",
    0)`` here read 0 for *every* worker and sub-agent. The loop then resolved the
    "balanced" context profile for a 400k model (half the tool-output inline
    limit a chat on the same model gets) and reported ``context_length=0`` /
    ``context_percent=0``, so a worker's context use was unobservable.

    An explicitly attached window wins — ``agent_tools/session_tools.py`` builds a
    runner namespace for a profile's model and may carry one. Otherwise ask the
    model, off the event loop because that probe does network I/O. On probe
    failure fall back to :data:`~src.model_context.DEFAULT_CONTEXT`, the same
    documented fallback ``get_context_length`` itself returns when a window can't
    be discovered — a 0 here is worse than a stated default. Scaling the input
    budget stays safe either way: the loop re-derives it through
    ``budget_context_for_model``, which returns 0 for an *unproven* window and so
    never budgets off this fallback.
    """
    from src.model_context import DEFAULT_CONTEXT, get_context_length

    try:
        attached = int(getattr(sess, "context_length", 0) or 0)
    except (TypeError, ValueError):
        attached = 0
    if attached > 0:
        return attached
    try:
        return int(await asyncio.to_thread(
            get_context_length, sess.endpoint_url, sess.model,
        ) or 0) or DEFAULT_CONTEXT
    except Exception:
        logger.debug("headless context-length probe failed for %s; using the default window",
                     getattr(sess, "model", "?"), exc_info=True)
        return DEFAULT_CONTEXT


def _rounds_exhausted_note(state: Dict[str, Any]) -> str:
    """The line a cut-off run hands back instead of looking finished.

    A headless caller only sees ``(prose, tool_events)``: the loop's
    ``rounds_exhausted`` frame is invisible to it. So a worker that spent its
    whole round budget mid-task returned exactly the shape of one that answered
    — and usually empty prose, because a cut-off final round is all tool calls
    and no text. The parent then saved "(no reply)" as the finished result and
    moved on without knowing there was anything left to do. Say what happened,
    and how far the run got, in the text itself.
    """
    events = state.get("tool_events") or []
    rounds = int(state.get("exhausted_rounds") or 0)
    last = next((ev.get("tool") for ev in reversed(events) if ev.get("tool")), "")
    done = (f"{len(events)} tool call{'' if len(events) == 1 else 's'} completed"
            f"{f', the last was {last}' if last else ''}") if events else "no tool calls completed"
    return (f"(ran out of rounds: the budget of {rounds} agent round{'' if rounds == 1 else 's'} was spent "
            f"with the task unfinished — {done}. Everything above is partial work, not a final answer; "
            "continue from there rather than starting over.)")

# Tools a headless child must never get: each one starts *another* agent, so
# without this a sub-agent could fan out sub-sub-agents without bound.
#
# `manage_agent_loadout` is in the set for its `action="start"`, which calls
# `agent_control.launch_worker` — it starts another agent, which is the whole
# reason this set exists. Its own gates do not stop a child: `caller_policy`
# hands a fresh child chat the "explicit" delegation default (only "never"
# refuses), and `live_children` is 0 because the child has started nothing yet,
# so both gates pass on the first call. The set is enforced by tool name, so
# the loadout CRUD actions go with it: a child that may not start a worker has
# no use for authoring one.
#
# `manage_agent_worktree` used to be in this set and is deliberately NOT any
# more. It starts no agent: it makes a checkout, commits in it and asks a human
# to publish, all in this process (agent_loop's delegation-gate comment makes
# the same call for the same tool). Blocking it here meant no worker could ever
# use it whatever its loadout granted, and nothing said so: on 2026-09-26 a
# "Lead Engineer" worker started on a repository task finished "Blocked before
# implementation ... this chat's tool policy denies manage_agent_worktree"
# while the chat that started it held the tool. Whether a worker has it is now
# its loadout's allowlist, like manage_git, bash or write_file; publishing is
# still behind the human approval code, and non-admins still never get it
# (NON_ADMIN_BLOCKED_TOOLS).
SUBAGENT_BLOCKED_TOOLS: frozenset = frozenset({
    "send_to_session", "create_session", "pipeline", "delegate_to_agent", "delegate_to_claude_code",
    "manage_session",
    # Both can start workers too: orchestrate_agents fans out specialists and
    # manage_agent_loadout's `start` launches one detached.
    "orchestrate_agents", "manage_agent_loadout",
})


# The two launchers a worker may keep when nesting is allowed. A lead engineer
# starting implementors needs exactly these.
NESTABLE_LAUNCH_TOOLS: frozenset = frozenset({"manage_agent_loadout", "orchestrate_agents"})

# Launchers a worker keeps under the same depth/policy rule as
# NESTABLE_LAUNCH_TOOLS, but only when its loadout names them outright
# (`tool_access: "selected"` with the name in `enabled_tools`) — never through
# `tool_access: "all"`, and never when the chat that started the worker may not
# use them itself.
#
# `delegate_to_claude_code` is here because a Lead Engineer whose job is to hand
# implementation to Claude Code could not do it: the tool was withheld from
# every worker at every depth, preflight read DEGRADED, and on 2026-09-28 the
# admin chat "repaired" the preset by dropping the tool the user had granted.
# A Claude Code run is a bounded leaf, not a fan-out:
#   - it runs `--restricted` with a fixed tool allowlist (read/edit/commit plus
#     named test runners; no push, fetch, remote, curl, env or arbitrary shell)
#     and a deny list that closes the prefix-match gaps (claude_code_tools);
#   - one run per checkout at a time (the per-repository lock) and at most
#     `claude_code_max_concurrent_tasks` across the instance (the process gate);
#   - each run or queued task counts against the worker's own child limit
#     (`agent_control.live_children`, checked by tool_execution's capacity gate);
#   - from a worker it runs only in that worker's workspace or a managed
#     worktree of the same repository (claude_code_tools._worker_repository);
#   - it cannot start an Odysseus agent: its only way back in is the callback
#     helper, whose /api/codex/* scopes cover todos, email, memory, calendar,
#     documents, vault and Cookbook — no chats, sessions or workers.
# It still counts as one more hop, which is why the depth rule applies: a
# worker at the depth limit, or with delegation "never", does not get it.
#
# The rest of SUBAGENT_BLOCKED_TOOLS (other chats, other agents, pipelines)
# stays off at every depth: each of those can start more Odysseus agents.
EXPLICIT_GRANT_LAUNCH_TOOLS: frozenset = frozenset({"delegate_to_claude_code"})
#: user chat (0) -> worker (1) -> sub-worker (2). Deeper is refused.
DEFAULT_MAX_WORKER_DEPTH = 2


def max_worker_depth() -> int:
    try:
        from src.settings import get_setting
        return max(1, min(4, int(get_setting("agent_max_worker_depth", DEFAULT_MAX_WORKER_DEPTH))))
    except Exception:
        return DEFAULT_MAX_WORKER_DEPTH


def worker_depth(session_id: Optional[str]) -> int:
    """How many worker hops ``session_id`` is below a chat a person started.

    Read from the ``parent_session`` each worker chat stores. A cycle or an
    unreadable row stops the walk; an absurdly long chain reads as deep, so a
    broken chain can only ever narrow what a worker may do.
    """
    from core.database import get_session_settings

    depth, seen, sid = 0, set(), str(session_id or "")
    while sid:
        if sid in seen or depth >= 10:
            return 10
        seen.add(sid)
        try:
            parent = (get_session_settings(sid) or {}).get("parent_session")
        except Exception:
            return 10
        if not parent:
            return depth
        depth += 1
        sid = str(parent)
    return depth


def child_blocked_tools(session_id: Optional[str], settings: Optional[Dict[str, Any]] = None) -> Set[str]:
    """What a worker chat may never call: every launcher, unless it may nest.

    A worker keeps ``NESTABLE_LAUNCH_TOOLS`` only while it is above the depth
    limit and its loadout's delegation policy is not ``never``. Whether it has
    those tools at all is still its loadout's allowlist, so nesting is opt-in
    per loadout (a lead engineer's grants manage_agent_loadout; an
    implementor's does not) and the parent chat's worker limit
    (``max_parallel_workers``) bounds the fan-out at each level.

    ``EXPLICIT_GRANT_LAUNCH_TOOLS`` follow the same rule, and additionally
    need the worker chat's stored allowlist to name them and the chat that
    started the worker to be allowed them itself.
    """
    settings = settings or {}
    blocked = blocked_at_depth(max(1, worker_depth(session_id)), settings.get("delegation_policy"),
                               tool_access=settings.get("tool_access"),
                               enabled_tools=settings.get("enabled_tools"))
    released = EXPLICIT_GRANT_LAUNCH_TOOLS - blocked
    if released:
        parent = settings.get("parent_session")
        if not parent and session_id:
            try:
                from core.database import get_session_settings

                parent = (get_session_settings(str(session_id)) or {}).get("parent_session")
            except Exception:
                parent = None
        blocked |= _denied_to_parent(parent, released)
    return blocked


def _denied_to_parent(parent_session: Optional[str], tools: Set[str]) -> Set[str]:
    """Which of ``tools`` the worker's parent chat may not use itself.

    The authoring clamp already refuses a loadout wider than the chat that
    writes it, but a saved loadout can be started by a different chat later.
    A worker must not hold a coding CLI its own parent is denied, so the
    parent's stored policy is read again here. Fails closed.
    """
    if not parent_session:
        return set(tools)
    try:
        from core.database import get_session_settings
        from src.session_settings import stored_disabled_tools

        denied = stored_disabled_tools(get_session_settings(str(parent_session)) or {})
    except Exception:
        logger.debug("worker launcher check: parent policy unreadable for %s", parent_session, exc_info=True)
        return set(tools)
    return {tool for tool in tools if tool in denied}


def explicitly_granted(tool_access: Optional[str], enabled_tools: Any) -> Set[str]:
    """The ``EXPLICIT_GRANT_LAUNCH_TOOLS`` a stored allowlist names outright."""
    if str(tool_access or "all").lower() != "selected":
        return set()
    names = {str(name).strip() for name in (enabled_tools or []) if str(name).strip()}
    return set(EXPLICIT_GRANT_LAUNCH_TOOLS & names)


def blocked_at_depth(depth: int, delegation_policy: Optional[str] = None, *,
                     tool_access: Optional[str] = None, enabled_tools: Any = None) -> Set[str]:
    """What a worker ``depth`` hops below a person's chat may never call.

    The rule :func:`child_blocked_tools` applies to a running worker, exposed
    so a loadout's preflight can say, before anything starts, which of the
    tools it grants its worker will not get. ``tool_access``/``enabled_tools``
    are the worker's own allowlist; without them no explicit-grant launcher is
    released.
    """
    if not may_nest(depth, delegation_policy):
        return set(SUBAGENT_BLOCKED_TOOLS)
    return (set(SUBAGENT_BLOCKED_TOOLS) - NESTABLE_LAUNCH_TOOLS
            - explicitly_granted(tool_access, enabled_tools))


def may_nest(depth: int, delegation_policy: Optional[str] = None) -> bool:
    """Whether a worker ``depth`` hops down may start one more hop of work."""
    policy = str(delegation_policy or "explicit").lower()
    return policy != "never" and max(1, int(depth)) < max_worker_depth()


# Why each worker-withheld tool is withheld, for a preflight or start message
# the model can act on instead of "the tool policy denies it".
def withheld_reason(tool: str, depth: Optional[int] = None, delegation_policy: Optional[str] = None) -> str:
    limit = max_worker_depth()
    policy = str(delegation_policy or "explicit").lower()
    if tool in EXPLICIT_GRANT_LAUNCH_TOOLS:
        if policy == "never":
            return ("the loadout's delegation_policy is 'never', and a Claude Code run is one more hop of "
                    "delegated work")
        if depth is not None and max(1, int(depth)) >= limit:
            return (f"the worker would run {max(1, int(depth))} level(s) below a person's chat, the limit "
                    f"(agent_max_worker_depth {limit}), and a Claude Code run is one more hop")
        return ("a worker gets it only when its loadout lists it in enabled_tools (tool_access 'selected'), "
                "above the depth limit, with delegation not 'never'")
    if tool in NESTABLE_LAUNCH_TOOLS:
        return ("it starts other workers, and a worker at this depth (or with delegation 'never') may not; "
                f"agent_max_worker_depth is {limit}")
    return ("it hands work to another chat or Agamemnon agent, which a worker may never do; "
            "do that part from the chat that starts the worker")


def withheld_repair(tool: str, depth: Optional[int] = None, delegation_policy: Optional[str] = None) -> str:
    """The one change that would give a worker ``tool``, or why none will.

    Never "drop it from enabled_tools": that edits what the user granted and
    changes nothing the worker can do.
    """
    policy = str(delegation_policy or "explicit").lower()
    if tool in EXPLICIT_GRANT_LAUNCH_TOOLS or tool in NESTABLE_LAUNCH_TOOLS:
        if policy == "never":
            return ("set the loadout's delegation_policy to 'explicit' or 'auto' (a widening: ask the user "
                    "first), or run that step from the chat that starts the worker")
        return ("start this loadout from a person's chat rather than from a worker, or have the user raise "
                f"agent_max_worker_depth (now {max_worker_depth()}); otherwise run that step from the chat "
                "that starts the worker. Leave the loadout as the user saved it")
    return ("run that step from the chat that starts the worker; no loadout setting gives a worker this "
            "tool. Leave the loadout as the user saved it")


def worker_tool_budget() -> int:
    """The per-run tool-call ceiling for a headless run; 0 = unlimited.

    The same `agent_max_tool_calls` setting and margin the chat route applies
    to a foreground turn. Headless runs used to pass nothing, so the ceiling
    every "rounds are advisory" comment names as a worker's real bound never
    applied to workers at all: a worker was observed running 108 rounds over
    23 minutes with nothing but the stall detector able to end it.
    """
    try:
        from src.settings import get_setting
        budget = int(get_setting("agent_max_tool_calls", 0) or 0)
    except (TypeError, ValueError):
        budget = 0
    except Exception:
        logger.debug("worker tool budget unavailable; running without one", exc_info=True)
        budget = 0
    return budget + max(10, budget // 10) if budget > 0 else 0


def _budget_exhausted_note(state: Dict[str, Any]) -> str:
    """The hand-back line for a run the tool-call ceiling cut off."""
    events = state.get("tool_events") or []
    limit = int(state.get("budget_limit") or 0)
    last = next((ev.get("tool") for ev in reversed(events) if ev.get("tool")), "")
    return (f"(ran out of tool calls: the per-run budget of {limit} was spent with the task unfinished"
            f"{f', the last call was {last}' if last else ''}. Everything above is partial work, not a "
            "final answer; continue from there rather than starting over.)")


async def run_headless(
    sess,
    messages: List[Dict[str, Any]],
    *,
    max_rounds: int = 12,
    max_tool_calls: Optional[int] = None,
    disabled_tools: Optional[Set[str]] = frozenset(),
    subagent: bool = True,
    deny_private_vault: bool = False,
    activity_session_id: Optional[str] = None,
    run_id: Optional[str] = None,
    source: str = "session",
    owner: Optional[str] = None,
    on_event: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    outcome: Optional[Dict[str, Any]] = None,
    workspace: Optional[str] = None,
    forced_tools: Optional[Set[str]] = None,
    wrap_up_round: int = 0,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Drain ``stream_agent_loop`` for ``sess`` over ``messages``.

    Returns ``(final_prose, tool_events)``. ``tool_events`` match the live
    chat's persisted shape so the frontend rebuilds them as ordinary
    agent-thread cards. When ``activity_session_id`` is given, every tool
    call is also published to that session's activity feed under ``run_id``.

    Two separate questions decide this run's policy, and they used to share one
    argument. ``disabled_tools`` is only *extra* denials the caller wants on top
    of everything else; ``None`` and ``frozenset()`` both mean "none supplied"
    and neither says anything about what kind of run this is. ``subagent`` says
    that: ``True`` (the default, because policy fails closed) for a detached
    child of another agent, which additionally gets
    :data:`SUBAGENT_BLOCKED_TOOLS` so it cannot fan out grandchildren; ``False``
    for a chat continuing *itself*, which instead runs under that chat's own
    stored tool policy. (The chat's ``approval_mode`` applies either way: it is
    resolved from the chat the run happens in, whichever kind of run it is.)

    ``deny_private_vault`` narrows only: it forces private-vault access off for
    this run whatever the chat's own grant says. It can never turn access on.

    Either way the owner's baseline (the global disabled-tools setting and their
    privileges) applies — these turns never pass through the chat route that
    enforces it.

    With a ``run_id``, :func:`request_stop` ends the run early; ``outcome``
    (when given) receives ``{"stopped": True}`` in that case. A run that instead
    burns through ``max_rounds`` while still working sets
    ``{"rounds_exhausted": True, "rounds": n}`` there, and says so in the prose
    it returns, so the caller can report a cut-off run as such rather than as a
    finished one.

    ``wrap_up_round`` (an agent profile's explicit ``max_rounds``; 0 = none) is
    passed to the loop, which makes that round a tool-free final answer. A run
    that wraps up that way and answers is a completed run, recorded in
    ``outcome`` as ``{"round_budget_reached": n}``; if the forced answer comes
    back empty it is reported as ``rounds_exhausted``, like a cut-off run.
    """
    effective_owner = owner if owner is not None else getattr(sess, "owner", None)
    # Foreground requests refresh session-backed provider credentials in the
    # chat route. Detached workers bypass that route, so perform the same
    # request-local refresh before fan-out rather than copying a stale bearer
    # into every child.
    try:
        from routes.chat_helpers import resolve_session_auth
        await asyncio.to_thread(resolve_session_auth, sess, str(getattr(sess, "id", "")), effective_owner)
    except Exception:
        logger.warning("headless provider credential preflight failed for %s",
                       getattr(sess, "id", "?"), exc_info=True)
    from src.tool_security import owner_baseline_disabled_tools
    from src.session_settings import effective_approval_mode, stored_disabled_tools
    try:
        from core.database import get_session_settings
        chat_settings = get_session_settings(getattr(sess, "id", None)) or {}
    except Exception:
        chat_settings = {}
    allow_private = bool(chat_settings.get("private_vault_access", False))
    if deny_private_vault:
        # A narrowing, never a grant: a caller may take private-vault access
        # away from this run, but may not hand it to a chat that has none.
        allow_private = False
    # The chat this run happens in decides what asks, for BOTH kinds of run. A
    # detached child's chat carries the mode its profile set when the child was
    # created; a chat continuing itself carries the user's own. The earlier
    # reading -- that a detached child should never be gated, because nobody is
    # there to answer -- is not taken here: a held call is recorded by
    # `tool_approvals.request` and listed by the Agent Control Room
    # (`routes/agents_routes.py`), so the question is answerable in both cases,
    # and a call that is never answered is one that never ran, which is the
    # direction policy is supposed to fail.
    approval_mode = effective_approval_mode(chat_settings)
    # The folder its file tools work in: the caller's (preflight's) choice,
    # else the one saved on the chat. Without it a worker had no workspace at
    # all and every repository task ended in "cannot read the repository".
    from src.tool_execution import vet_workspace
    workspace = vet_workspace(workspace or chat_settings.get("workspace") or "") or None

    baseline = owner_baseline_disabled_tools(effective_owner)
    extra = set(disabled_tools or ())
    if subagent:
        blocked = child_blocked_tools(getattr(sess, "id", None), chat_settings) | extra | baseline
    else:
        # A chat continuing ITSELF — after a worker it launched finished, or
        # after a background job it started did. It is the user's own chat, so
        # it runs under the user's own chat policy. Not doing this is how a
        # chat with `bash` switched off came to run `bash` the moment a worker
        # reported back.
        #
        # This run continues the user's foreground chat, in that chat, and
        # writes its answer into that chat's transcript, so the chat's own
        # `approval_mode` (resolved above for every headless run) is exactly
        # right here: a chat set to `ask_all` keeps asking.
        #
        # SUBAGENT_BLOCKED_TOOLS is deliberately NOT added here. It exists to
        # stop a *child* minting grandchildren; the parent delegating is the
        # normal case, not the thing being prevented.
        blocked = extra | stored_disabled_tools(chat_settings) | baseline
        if chat_settings.get("parent_session"):
            # A WORKER's chat continuing itself after its own sub-workers
            # finished (a lead engineer picking up its implementors' results).
            # It is still a worker: the depth rule applies to this run too.
            blocked |= child_blocked_tools(getattr(sess, "id", None), chat_settings)
    blocked = blocked or None

    state: Dict[str, Any] = {"full": "", "tool_events": [], "round": 1}
    stop_event = asyncio.Event() if run_id else None
    if run_id and stop_event is not None:
        _STOP_EVENTS[run_id] = stop_event
        _STEER_RUNS.setdefault(str(getattr(sess, "id", "")), set()).add(run_id)
    if max_tool_calls is None:
        max_tool_calls = worker_tool_budget()
    drain = asyncio.ensure_future(_drain(sess, messages, state, max_rounds=max_rounds,
                                         max_tool_calls=max_tool_calls, owner=effective_owner,
                                         blocked=blocked, activity_session_id=activity_session_id,
                                         run_id=run_id, source=source, on_event=on_event,
                                         allow_private=allow_private, approval_mode=approval_mode,
                                         workspace=workspace, forced_tools=forced_tools,
                                         wrap_up_round=wrap_up_round))
    try:
        if stop_event is None:
            await drain
        else:
            stopper = asyncio.ensure_future(stop_event.wait())
            try:
                done, _ = await asyncio.wait({drain, stopper}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                stopper.cancel()
            if drain in done:
                drain.result()  # re-raise a failure
            else:
                drain.cancel()
                try:
                    await drain
                except asyncio.CancelledError:
                    pass
                if outcome is not None:
                    outcome["stopped"] = True
                if activity_session_id:
                    activity.publish(activity_session_id, "status",
                                     f"Stopped {_STOP_SOURCES.get(run_id, _DEFAULT_STOP_SOURCE)}",
                                     source=source, run_id=run_id, owner=effective_owner,
                                     data={"status": "cancelled"}, level="warning")
    except asyncio.CancelledError:
        drain.cancel()
        raise
    finally:
        if run_id:
            try:
                from src import agent_control
                agent_control.clear_steer(getattr(sess, "id", None), run_id=run_id)
            except Exception:
                logger.debug("headless steer cleanup failed", exc_info=True)
        if run_id and _STOP_EVENTS.get(run_id) is stop_event:
            _STOP_EVENTS.pop(run_id, None)
        if run_id:
            session_runs = _STEER_RUNS.get(str(getattr(sess, "id", "")))
            if session_runs is not None:
                session_runs.discard(run_id)
                if not session_runs:
                    _STEER_RUNS.pop(str(getattr(sess, "id", "")), None)
        _record_usage(sess, state, effective_owner)
    full = state["full"]
    stopped_by = _STOP_SOURCES.pop(run_id, _DEFAULT_STOP_SOURCE) if run_id else _DEFAULT_STOP_SOURCE
    if outcome is not None and outcome.get("stopped"):
        full = (full.rstrip() + "\n\n" if full.strip() else "") + f"(stopped {stopped_by} before finishing)"
    elif state.get("exhausted"):
        # A stop wins when both could apply — the user ending a run is the more
        # specific fact, and a cancelled drain never sees the loop's frame anyway.
        if outcome is not None:
            outcome["rounds_exhausted"] = True
            outcome["rounds"] = int(state.get("exhausted_rounds") or 0)
        full = (full.rstrip() + "\n\n" if full.strip() else "") + _rounds_exhausted_note(state)
    elif state.get("budget_exhausted"):
        # Reported through the same `rounds_exhausted` flag so every caller
        # already records the run as incomplete rather than finished.
        if outcome is not None:
            outcome["rounds_exhausted"] = True
            outcome["budget_exhausted"] = True
            outcome["rounds"] = int(state.get("round") or 0)
        full = (full.rstrip() + "\n\n" if full.strip() else "") + _budget_exhausted_note(state)
    elif state.get("awaiting_approval"):
        # The run ended on an approval card, not because the work is done. Say
        # so to whoever reads the result, and let the caller record the run as
        # waiting rather than completed.
        pending = state["awaiting_approval"]
        if outcome is not None:
            outcome["awaiting_approval"] = dict(pending)
        full = ((full.rstrip() + "\n\n" if full.strip() else "")
                + f"(paused: {pending.get('tool') or 'a tool call'} is waiting for the user's approval "
                "in this worker's chat; the task is not finished)")
    elif state.get("round_budget") and outcome is not None:
        # Wrapped up at the profile's round budget and wrote its answer: a
        # finished run whose text says what is left, not a cut-off one.
        outcome["round_budget_reached"] = int(state["round_budget"])
    return full, state["tool_events"]


def _record_usage(sess, state: Dict[str, Any], owner: Optional[str]) -> None:
    """Add this run's tokens to its chat's totals and the usage ledger.

    The chat route does this for foreground turns. Worker, background-job and
    continuation turns run here instead and were never counted: on 2026-10-07
    a worker that read 28M input tokens showed 0 on its chat.
    """
    metrics = dict(state.get("metrics") or {})
    if not metrics.get("input_tokens") and not metrics.get("output_tokens"):
        # Stopped or failed before the loop's final metrics frame: fall back to
        # the live per-round counters.
        progress = state.get("progress") or {}
        metrics = {"input_tokens": progress.get("input_tokens", 0),
                   "output_tokens": progress.get("output_tokens", 0),
                   "cached_input_tokens": progress.get("cached_tokens", 0),
                   "agent_rounds": progress.get("round", 0),
                   "model": getattr(sess, "model", "")}
    metrics.setdefault("model", getattr(sess, "model", ""))
    try:
        from routes.chat_helpers import accumulate_token_usage

        accumulate_token_usage(str(getattr(sess, "id", "")), metrics, owner=owner,
                               incognito=bool(getattr(sess, "incognito", False)))
    except Exception:
        logger.debug("headless usage accounting failed", exc_info=True)


# Loop frames that move a run's live counters (see ``_track_progress``).
_PROGRESS_EVENTS = frozenset({"agent_step", "tool_start", "tool_output", "round_usage"})


def _track_progress(run_id: str, d: Dict[str, Any], state: Dict[str, Any]) -> None:
    """Fold one loop frame into the run's live ``progress`` record.

    A detached run was otherwise visible only as its last event title: the
    round it was on, how many tokens it had burned and how much of that the
    provider cached lived in the log alone, which is how a 108-round worker
    looked no different from a 3-round one. Totals are kept on ``state`` so
    they cover the whole run, and are written through ``note_progress`` (no
    event, bounded saves).
    """
    kind = d.get("type")
    if kind not in _PROGRESS_EVENTS:
        return
    p = state.setdefault("progress", {"round": 1, "tool_calls": 0, "current_tool": None,
                                      "input_tokens": 0, "cached_tokens": 0, "output_tokens": 0})
    if kind == "agent_step":
        p["round"] = d.get("round", p["round"])
    elif kind == "tool_start":
        p["current_tool"] = d.get("tool")
    elif kind == "tool_output":
        p["current_tool"] = None
        p["tool_calls"] += 1
    else:
        for key, total in (("input", "input_tokens"), ("cached", "cached_tokens"), ("output", "output_tokens")):
            try:
                p[total] += int(d.get(key) or 0)
            except (TypeError, ValueError):
                pass
    p["last_event_at"] = time.time()
    activity.note_progress(run_id, **p)


async def _drain(sess, messages, state: Dict[str, Any], *, max_rounds: int, owner: Optional[str],
                 max_tool_calls: int = 0,
                 blocked: Optional[Set[str]], activity_session_id: Optional[str], run_id: Optional[str],
                 source: str, on_event, allow_private: bool = False,
                 approval_mode: Optional[str] = None, workspace: Optional[str] = None,
                 forced_tools: Optional[Set[str]] = None, wrap_up_round: int = 0) -> None:
    from src.agent_loop import stream_agent_loop

    tool_events: List[Dict[str, Any]] = state["tool_events"]
    round_num = state["round"]
    async for chunk in stream_agent_loop(
        sess.endpoint_url, sess.model, messages,
        headers=getattr(sess, "headers", None),
        context_length=await _resolve_context_length(sess),
        session_id=sess.id,
        # Keep a headless worker's steering queue attached to its externally
        # visible run.  Without this a foreground turn in the same session can
        # drain or cancel the worker's correction (and vice versa).
        steer_run_id=run_id,
        max_rounds=max_rounds,
        max_tool_calls=max_tool_calls,
        owner=owner,
        disabled_tools=blocked,
        allow_private=allow_private,
        approval_mode=approval_mode,
        workspace=workspace,
        forced_tools=set(forced_tools) if forced_tools else None,
        wrap_up_round=wrap_up_round,
    ):
        event_error = _sse_error_payload(chunk)
        if event_error is not None:
            error = _stream_error(event_error)
            state["stream_error"] = {
                "message": str(error), "status": error.status, "retryable": error.retryable,
            }
            if activity_session_id:
                activity.publish(activity_session_id, "status", "Upstream model request failed",
                                 source=source, run_id=run_id, owner=owner,
                                 detail=str(error)[:2000],
                                 data={"status": "failed", "http_status": error.status,
                                       "retryable": error.retryable}, level="error")
            raise error
        if not chunk.startswith("data: "):
            continue
        body = chunk[6:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            d = json.loads(body)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        if d.get("type") == "stream_error":
            error = _stream_error(d)
            state["stream_error"] = {
                "message": str(error), "status": error.status, "retryable": error.retryable,
            }
            raise error
        if on_event is not None:
            try:
                await on_event(d)
            except Exception:  # a listener must never stop the drain
                logger.debug("headless agent listener failed", exc_info=True)
        if run_id:
            _track_progress(run_id, d, state)
        if d.get("type") == "metrics" and isinstance(d.get("data"), dict):
            state["metrics"] = d["data"]
        if "delta" in d:
            delta = d.get("delta")
            if isinstance(delta, str) and not d.get("thinking"):
                state["full"] += delta
        elif d.get("type") == "steer_applied":
            # Detached/headless loops do not pass through chat_routes' SSE
            # persistence branch. Preserve the same human/peer attribution in
            # both paths, so a resumed worker never renders peer mail as "You".
            try:
                from src.agent_control import persist_applied_steer, persist_steer_split
                # Same split as the chat route (routes/chat_routes.py): what the
                # worker produced before this steer is its own assistant message,
                # so the history reads in the order things happened and the
                # caller saves only what comes after.
                split_round = int(d.get("round") or 0)
                if split_round > state.get("split_round", 1):
                    state["split_round"] = split_round
                    if state["full"].strip() or tool_events:
                        meta = {"steer_split_round": split_round, "steer_id": d.get("steer_id")}
                        if tool_events:
                            meta["tool_events"] = list(tool_events)
                        persist_steer_split(sess, content=state["full"].strip(), metadata=meta)
                        state["full"] = ""
                        del tool_events[:]
                persist_applied_steer(sess, d)
            except Exception:
                logger.debug("headless steer persistence failed", exc_info=True)
        elif d.get("type") == "agent_step":
            round_num = d.get("round", round_num)
            state["round"] = round_num
        elif d.get("type") == "rounds_exhausted":
            # The loop hit its round cap while still working. A live chat turns
            # this into a "Continue" button; a headless run has nobody to press
            # it, so record it for the hand-back note run_headless appends.
            state["exhausted"] = True
            state["exhausted_rounds"] = int(d.get("rounds") or 0)
            if activity_session_id:
                # A note, not a `status` event: the caller decides the run's
                # final status, and a status event here would be overwritten by
                # the run_finished that follows anyway.
                activity.publish(activity_session_id, "note",
                                 f"Ran out of rounds after {state['exhausted_rounds']} — handing back partial work",
                                 source=source, run_id=run_id, owner=owner,
                                 data={"rounds": state["exhausted_rounds"],
                                       "tool_calls": d.get("tool_calls")},
                                 level="warning")
        elif d.get("type") == "round_budget_reached":
            # The profile's round budget: the loop runs this round without
            # tools so the worker writes up what it has and what is left.
            state["round_budget"] = int(d.get("budget") or 0)
            if activity_session_id:
                activity.publish(activity_session_id, "note",
                                 f"Reached its round budget of {state['round_budget']} — wrapping up with what it has",
                                 source=source, run_id=run_id, owner=owner,
                                 data={"rounds": state["round_budget"], "round": d.get("round")})
        elif d.get("type") == "round_budget_unanswered":
            # The wrap-up round wrote nothing of its own (the loop filled in a
            # canned line), so hand back as incomplete, like a cut-off run.
            state["exhausted"] = True
            state["exhausted_rounds"] = int(d.get("budget") or 0)
        elif d.get("type") == "budget_exceeded":
            # The loop stops right after this frame with no closing answer, so
            # the run must hand back as incomplete, like a rounds_exhausted one.
            state["budget_exhausted"] = True
            state["budget_limit"] = int(d.get("limit") or 0)
            if activity_session_id:
                activity.publish(activity_session_id, "note",
                                 f"Ran out of tool calls after {state['budget_limit']} — handing back partial work",
                                 source=source, run_id=run_id, owner=owner,
                                 data={"limit": state["budget_limit"], "used": d.get("used")},
                                 level="warning")
        elif d.get("type") == "tool_start" and activity_session_id:
            activity.publish(activity_session_id, "tool_start",
                             f"{d.get('tool')} {str(d.get('command') or '')[:120]}".strip(),
                             source=source, run_id=run_id, owner=owner,
                             data={"tool": d.get("tool"), "round": round_num})
        elif d.get("type") == "tool_output":
            ev = {
                "round": round_num,
                "tool": d.get("tool"),
                "command": d.get("command"),
                "output": d.get("output"),
                "exit_code": d.get("exit_code"),
            }
            if d.get("diff"):
                ev["diff"] = d.get("diff")
            # The work record a resumed worker reads (src/turn_trail.py) names
            # the documents it created or changed.
            if d.get("doc_id"):
                ev["doc_id"] = d.get("doc_id")
            approval = d.get("ask_user")
            if isinstance(approval, dict):
                # Saved with the reply, so the worker's chat shows the
                # approval card and the user can decide it there.
                ev["ask_user"] = approval
                if approval.get("approval_id"):
                    state["awaiting_approval"] = {"approval_id": approval.get("approval_id"),
                                                  "tool": d.get("tool")}
                if activity_session_id and approval.get("approval_id"):
                    activity.publish(activity_session_id, "status",
                                     f"Waiting for approval: {d.get('tool')}",
                                     source=source, run_id=run_id, owner=owner,
                                     detail=str(approval.get("description") or "")[:2000] or None,
                                     data={"approval_id": approval.get("approval_id"), "tool": d.get("tool"),
                                           "target_session": str(getattr(sess, "id", ""))},
                                     level="warning")
            tool_events.append(ev)
            if activity_session_id:
                failed = d.get("exit_code") not in (0, None)
                activity.publish(activity_session_id, "tool_result",
                                 f"{d.get('tool')} {'failed' if failed else 'done'}",
                                 source=source, run_id=run_id, owner=owner,
                                 detail=str(d.get("output") or "")[:2000] or None,
                                 data={"tool": d.get("tool"), "exit_code": d.get("exit_code"), "round": round_num},
                                 level="error" if failed else "info")
