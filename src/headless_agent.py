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

import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from src import agent_activity as activity

logger = logging.getLogger(__name__)

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
) -> Tuple[str, List[Dict[str, Any]]]:
    """Drain ``stream_agent_loop`` for ``sess`` over ``messages``.

    Returns ``(final_prose, tool_events)``. ``tool_events`` match the live
    chat's persisted shape so the frontend rebuilds them as ordinary
    agent-thread cards. When ``activity_session_id`` is given, every tool
    call is also published to that session's activity feed under ``run_id``.

    ``disabled_tools`` is added to :data:`SUBAGENT_BLOCKED_TOOLS`; pass
    ``None`` to block nothing at all (a chat continuing itself, not a child).
    """
    from src.agent_loop import stream_agent_loop

    full = ""
    tool_events: List[Dict[str, Any]] = []
    round_num = 1
    blocked = None if disabled_tools is None else (set(SUBAGENT_BLOCKED_TOOLS) | set(disabled_tools))
    async for chunk in stream_agent_loop(
        sess.endpoint_url, sess.model, messages,
        headers=getattr(sess, "headers", None),
        context_length=getattr(sess, "context_length", 0) or 0,
        session_id=sess.id,
        max_rounds=max_rounds,
        owner=owner if owner is not None else getattr(sess, "owner", None),
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
                full += delta
        elif d.get("type") == "agent_step":
            round_num = d.get("round", round_num)
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
    return full, tool_events
