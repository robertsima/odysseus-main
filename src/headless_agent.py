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

# Tools a headless child must never get: each one starts *another* agent, so
# without this a sub-agent could fan out sub-sub-agents without bound.
SUBAGENT_BLOCKED_TOOLS: frozenset = frozenset({
    "send_to_session", "create_session", "pipeline", "delegate_to_claude_code",
    "manage_session", "manage_agent_worktree",
})


async def run_headless(
    sess,
    messages: List[Dict[str, Any]],
    *,
    max_rounds: int = 12,
    disabled_tools: Optional[Set[str]] = frozenset(),
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

    ``disabled_tools`` is added to :data:`SUBAGENT_BLOCKED_TOOLS`; pass
    ``None`` for a chat continuing itself rather than a child. Either way the
    owner's baseline (the global disabled-tools setting and their privileges)
    applies — these turns never pass through the chat route that enforces it.

    With a ``run_id``, :func:`request_stop` ends the run early; ``outcome``
    (when given) receives ``{"stopped": True}`` in that case.
    """
    effective_owner = owner if owner is not None else getattr(sess, "owner", None)
    from src.tool_security import owner_baseline_disabled_tools

    baseline = owner_baseline_disabled_tools(effective_owner)
    if disabled_tools is None:
        blocked = baseline or None
    else:
        blocked = set(SUBAGENT_BLOCKED_TOOLS) | set(disabled_tools) | baseline

    state: Dict[str, Any] = {"full": "", "tool_events": [], "round": 1}
    stop_event = asyncio.Event() if run_id else None
    if run_id and stop_event is not None:
        _STOP_EVENTS[run_id] = stop_event
    drain = asyncio.ensure_future(_drain(sess, messages, state, max_rounds=max_rounds, owner=effective_owner,
                                         blocked=blocked, activity_session_id=activity_session_id,
                                         run_id=run_id, source=source, on_event=on_event))
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
    return full, state["tool_events"]


async def _drain(sess, messages, state: Dict[str, Any], *, max_rounds: int, owner: Optional[str],
                 blocked: Optional[Set[str]], activity_session_id: Optional[str], run_id: Optional[str],
                 source: str, on_event) -> None:
    from src.agent_loop import stream_agent_loop

    tool_events: List[Dict[str, Any]] = state["tool_events"]
    round_num = state["round"]
    async for chunk in stream_agent_loop(
        sess.endpoint_url, sess.model, messages,
        headers=getattr(sess, "headers", None),
        context_length=getattr(sess, "context_length", 0) or 0,
        session_id=sess.id,
        max_rounds=max_rounds,
        owner=owner,
        disabled_tools=blocked,
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
