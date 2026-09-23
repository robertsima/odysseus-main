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
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from src import agent_activity as activity

logger = logging.getLogger(__name__)

# run_id → stop event for every headless run in flight, so the UI can stop one
# sub-agent without stopping the parent turn that is waiting on it. A stopped
# run returns what it produced so far, which re-enters the parent as the tool
# result (the partial work is not thrown away).
_STOP_EVENTS: Dict[str, asyncio.Event] = {}


def request_stop(run_id: str) -> bool:
    """Ask a running headless run to stop. Returns False if none is running."""
    event = _STOP_EVENTS.get(run_id)
    if event is None:
        return False
    event.set()
    return True


def running_ids() -> Set[str]:
    return set(_STOP_EVENTS)


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
# the loadout CRUD actions go with it; a child that may not start a worker has
# no use for authoring one.
SUBAGENT_BLOCKED_TOOLS: frozenset = frozenset({
    "send_to_session", "create_session", "pipeline", "delegate_to_agent", "delegate_to_claude_code",
    "manage_session", "manage_agent_worktree", "manage_agent_loadout",
})


async def run_headless(
    sess,
    messages: List[Dict[str, Any]],
    *,
    max_rounds: int = 12,
    disabled_tools: Optional[Set[str]] = frozenset(),
    subagent: bool = True,
    deny_private_vault: bool = False,
    activity_session_id: Optional[str] = None,
    run_id: Optional[str] = None,
    source: str = "session",
    owner: Optional[str] = None,
    on_event: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    outcome: Optional[Dict[str, Any]] = None,
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
    stored policy — the tools it has switched off and its approval mode.

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
    """
    effective_owner = owner if owner is not None else getattr(sess, "owner", None)
    from src.session_settings import effective_approval_mode, stored_disabled_tools
    from src.tool_security import owner_baseline_disabled_tools
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

    baseline = owner_baseline_disabled_tools(effective_owner)
    extra = set(disabled_tools or ())
    if subagent:
        blocked = set(SUBAGENT_BLOCKED_TOOLS) | extra | baseline
        # A detached child runs in a chat the user did not open; an approval
        # card raised there really would have nobody to answer it.
        approval_mode: Optional[str] = None
    else:
        # A chat continuing ITSELF — after a worker it launched finished, or
        # after a background job it started did. It is the user's own chat, so
        # it runs under the user's own chat policy. Not doing this is how a
        # chat with `bash` switched off came to run `bash` the moment a worker
        # reported back.
        #
        # Resolving the tension with `src/agent_loop.py`'s note that headless
        # callers "are never gated — nobody would be there to answer": that is
        # right for the branch above and wrong for this one. This run continues
        # the user's foreground chat, in that chat, and writes its answer into
        # that chat's transcript; the user is exactly as present as they were
        # for the turn that launched the worker. So a chat set to `ask_all`
        # keeps asking. A gated call is recorded by `tool_approvals.request`
        # and listed by the Agent Control Room (`routes/agents_routes.py`), so
        # the question is answerable rather than dropped — and a call that is
        # never answered is one that never ran, which is the direction policy
        # is supposed to fail.
        #
        # SUBAGENT_BLOCKED_TOOLS is deliberately NOT added here. It exists to
        # stop a *child* minting grandchildren; the parent delegating is the
        # normal case, not the thing being prevented.
        blocked = extra | stored_disabled_tools(chat_settings) | baseline
        approval_mode = effective_approval_mode(chat_settings)
    blocked = blocked or None

    state: Dict[str, Any] = {"full": "", "tool_events": [], "round": 1}
    stop_event = asyncio.Event() if run_id else None
    if run_id and stop_event is not None:
        _STOP_EVENTS[run_id] = stop_event
    drain = asyncio.ensure_future(_drain(sess, messages, state, max_rounds=max_rounds, owner=effective_owner,
                                         blocked=blocked, activity_session_id=activity_session_id,
                                         run_id=run_id, source=source, on_event=on_event,
                                         allow_private=allow_private, approval_mode=approval_mode))
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
                    activity.publish(activity_session_id, "status", "Stopped by the user",
                                     source=source, run_id=run_id, owner=effective_owner,
                                     data={"status": "cancelled"}, level="warning")
    except asyncio.CancelledError:
        drain.cancel()
        raise
    finally:
        if run_id and _STOP_EVENTS.get(run_id) is stop_event:
            _STOP_EVENTS.pop(run_id, None)
    full = state["full"]
    if outcome is not None and outcome.get("stopped"):
        full = (full.rstrip() + "\n\n" if full.strip() else "") + "(stopped by the user before finishing)"
    elif state.get("exhausted"):
        # A stop wins when both could apply — the user ending a run is the more
        # specific fact, and a cancelled drain never sees the loop's frame anyway.
        if outcome is not None:
            outcome["rounds_exhausted"] = True
            outcome["rounds"] = int(state.get("exhausted_rounds") or 0)
        full = (full.rstrip() + "\n\n" if full.strip() else "") + _rounds_exhausted_note(state)
    return full, state["tool_events"]


async def _drain(sess, messages, state: Dict[str, Any], *, max_rounds: int, owner: Optional[str],
                 blocked: Optional[Set[str]], activity_session_id: Optional[str], run_id: Optional[str],
                 source: str, on_event, allow_private: bool = False,
                 approval_mode: Optional[str] = None) -> None:
    from src.agent_loop import stream_agent_loop

    tool_events: List[Dict[str, Any]] = state["tool_events"]
    round_num = state["round"]
    async for chunk in stream_agent_loop(
        sess.endpoint_url, sess.model, messages,
        headers=getattr(sess, "headers", None),
        context_length=await _resolve_context_length(sess),
        session_id=sess.id,
        max_rounds=max_rounds,
        owner=owner,
        disabled_tools=blocked,
        allow_private=allow_private,
        approval_mode=approval_mode,
    ):
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
        if on_event is not None:
            try:
                await on_event(d)
            except Exception:  # a listener must never stop the drain
                logger.debug("headless agent listener failed", exc_info=True)
        if "delta" in d:
            delta = d.get("delta")
            if isinstance(delta, str) and not d.get("thinking"):
                state["full"] += delta
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
            tool_events.append(ev)
            if activity_session_id:
                failed = d.get("exit_code") not in (0, None)
                activity.publish(activity_session_id, "tool_result",
                                 f"{d.get('tool')} {'failed' if failed else 'done'}",
                                 source=source, run_id=run_id, owner=owner,
                                 detail=str(d.get("output") or "")[:2000] or None,
                                 data={"tool": d.get("tool"), "exit_code": d.get("exit_code"), "round": round_num},
                                 level="error" if failed else "info")
