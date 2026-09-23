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


def request_stop(run_id: str) -> bool:
    """Ask a running headless run to stop. Returns False if none is running."""
    event = _STOP_EVENTS.get(run_id)
    if event is None:
        return False
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
SUBAGENT_BLOCKED_TOOLS: frozenset = frozenset({
    "send_to_session", "create_session", "pipeline", "delegate_to_agent", "delegate_to_claude_code",
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
    workspace: Optional[str] = None,
    forced_tools: Optional[Set[str]] = None,
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
    (when given) receives ``{"stopped": True}`` in that case. A run that instead
    burns through ``max_rounds`` while still working sets
    ``{"rounds_exhausted": True, "rounds": n}`` there, and says so in the prose
    it returns, so the caller can report a cut-off run as such rather than as a
    finished one.
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
    from src.session_settings import effective_approval_mode
    try:
        from core.database import get_session_settings
        chat_settings = get_session_settings(sess.id) or {}
    except Exception:
        chat_settings = {}
    allow_private = bool(chat_settings.get("private_vault_access", False))
    # The worker's own chat decides what asks (a profile's mode is stored
    # there when the child is created), else the app-wide default.
    approval_mode = effective_approval_mode(chat_settings)
    # The folder its file tools work in: the caller's (preflight's) choice,
    # else the one saved on the chat. Without it a worker had no workspace at
    # all and every repository task ended in "cannot read the repository".
    from src.tool_execution import vet_workspace
    workspace = vet_workspace(workspace or chat_settings.get("workspace") or "") or None

    baseline = owner_baseline_disabled_tools(effective_owner)
    if disabled_tools is None:
        blocked = baseline or None
    else:
        blocked = set(SUBAGENT_BLOCKED_TOOLS) | set(disabled_tools) | baseline

    state: Dict[str, Any] = {"full": "", "tool_events": [], "round": 1}
    stop_event = asyncio.Event() if run_id else None
    if run_id and stop_event is not None:
        _STOP_EVENTS[run_id] = stop_event
        _STEER_RUNS.setdefault(str(getattr(sess, "id", "")), set()).add(run_id)
    drain = asyncio.ensure_future(_drain(sess, messages, state, max_rounds=max_rounds, owner=effective_owner,
                                         blocked=blocked, activity_session_id=activity_session_id,
                                         run_id=run_id, source=source, on_event=on_event,
                                         allow_private=allow_private, approval_mode=approval_mode,
                                         workspace=workspace, forced_tools=forced_tools))
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
    return full, state["tool_events"]


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
                 blocked: Optional[Set[str]], activity_session_id: Optional[str], run_id: Optional[str],
                 source: str, on_event, allow_private: bool = False,
                 approval_mode: Optional[str] = None, workspace: Optional[str] = None,
                 forced_tools: Optional[Set[str]] = None) -> None:
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
        owner=owner,
        disabled_tools=blocked,
        allow_private=allow_private,
        approval_mode=approval_mode,
        workspace=workspace,
        forced_tools=set(forced_tools) if forced_tools else None,
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
        if "delta" in d:
            delta = d.get("delta")
            if isinstance(delta, str) and not d.get("thinking"):
                state["full"] += delta
        elif d.get("type") == "steer_applied":
            # Detached/headless loops do not pass through chat_routes' SSE
            # persistence branch. Preserve the same human/peer attribution in
            # both paths, so a resumed worker never renders peer mail as "You".
            try:
                from src.agent_control import persist_applied_steer
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
