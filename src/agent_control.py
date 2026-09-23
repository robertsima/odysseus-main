"""Control surface for running agents: steer, stop, and launch workers.

Backs the Agents dashboard (routes/agents_routes.py). Everything here is
owner-scoped by the routes; this module only knows run ids and session ids.

* **Steer** — queue a message for a running turn. The agent loop drains the
  queue at the start of each round and appends it as a user message, so the
  correction lands mid-task instead of after the turn ends. Every message has
  an id and a visible state (``queued`` → ``acknowledged`` → ``injected``,
  or ``cancelled``/``failed``); the transitions go to the activity feed, which
  is where the history of a steer lives once it has left the queue. See the
  block comment above ``STEER_STATES`` for what each state is evidence of —
  and for the two states this module refuses to invent.
* **Stop** — end one unit of work (a sub-agent, Claude Code task, background
  job, or a chat turn) without stopping anything else.
* **Launch** — start a named worker profile in a fresh chat as a detached
  background run, so the user can spin agents up from the dashboard rather
  than only through another agent's tool call.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from src import agent_activity as activity

logger = logging.getLogger(__name__)


# Workers get one run, never extra "continuation legs". Those were added when a
# round ceiling cut workers off mid-task; the loop has since dropped round
# ceilings altogether (stream_agent_loop treats max_rounds as advisory), so the
# legs could only turn an explicit budget into up to five of them. A run is
# bounded by progress instead: the stall and runaway detectors, the per-run
# tool-call ceiling, the request timeout and the stop control. If a caller ever
# reports rounds_exhausted, the run ends as incomplete and is not extended.


def live_children(session_id: Optional[str]) -> int:
    """Child runs currently in flight for ``session_id``.

    The per-chat ``max_parallel_workers`` limit is checked in two places — the
    spawning-tool gate in :mod:`src.tool_execution` and the loadout tool's
    ``start`` action — so the counting rule lives here rather than being
    written twice and drifting. ``odysseus`` runs are the chat's own turns, not
    children, so they do not count against the limit. Claude Code tasks are
    registered with their runner before they enter ``_run_claude`` and publish
    an activity run. Include those queued records as well: otherwise several
    same-round starts can all pass this gate while they wait on Claude's
    process semaphore. Once activity has the run, the task id/run id overlap
    is de-duplicated.
    """
    if not session_id:
        return 0
    sid = str(session_id)
    try:
        active = [
            rec for rec in activity.list_runs(limit=400)
            if (rec.get("session_id") == sid or (rec.get("summary") or {}).get("parent_session") == sid)
            and rec.get("status") == "running"
            and rec.get("source") != "odysseus"
            and not (rec.get("summary") or {}).get("workflow_controller")
        ]
    except Exception:
        active = []

    # ``task_id`` is the runner's id and is used as the activity run id by
    # _run_claude. Keep both forms for old activity rows that only recorded one
    # of them in their summary.
    represented = set()
    for rec in active:
        if rec.get("run_id"):
            represented.add(str(rec["run_id"]))
        summary = rec.get("summary") or {}
        if isinstance(summary, dict) and summary.get("task_id"):
            represented.add(str(summary["task_id"]))

    total = len(active)
    try:
        # Lazy to avoid importing the Claude integration (which imports the
        # activity subsystem) on ordinary chat/delegation paths.
        from src.agent_tools.claude_code_tools import get_task_runner

        for task in get_task_runner().summaries(limit=400):
            if (task.get("session_id") == sid
                    and task.get("status") in {"queued", "running"}
                    and str(task.get("task_id") or "") not in represented):
                total += 1
    except Exception:
        logger.debug("Could not include queued Claude Code tasks in child count", exc_info=True)
    return total


# ── steering ──────────────────────────────────────────────────────────────

# A steering message is queued here and drained by the agent loop between
# rounds. That used to be the whole of it: session -> list of records, no ids,
# no states, nothing written down. Once a message left the queue there was no
# way to answer the only question anyone actually asks about steering — "did
# it land?" — because an empty queue looks identical whether the loop read the
# message or the turn ended and threw it away.
#
# So every message now carries an id and a state, and every transition is
# published to the activity feed (src/agent_activity.py). The feed is the
# history store, deliberately: it already persists one JSONL file per session,
# already survives a restart, already fans out to the Control Room's SSE
# stream, and is already owner-scoped by the routes that expose it. A second
# store would have to re-earn all four and then be kept consistent with the
# feed the same UI reads beside it.
#
# The states below are the ones this code can actually WITNESS:
#
#   queued        steer() accepted the message into the queue.
#   acknowledged  a running turn drained it (drain_steer_records) — proof that
#                 a live loop took ownership, which is the one thing the queue
#                 alone can never tell you.
#   injected      the loop appended it to the model's messages for a numbered
#                 round. This is the last honest point of observation.
#   cancelled     the turn ended with the message still queued, so it was
#                 dropped rather than left to ambush an unrelated later turn
#                 (clear_steer).
#   failed        it never made it into a turn, with a reason: the queue was
#                 full, the target was not running or not steerable, or it was
#                 drained and then found unusable.
#
# Two states from the original spec are deliberately NOT emitted:
#
#   completed     nothing here observes a steering message being *carried
#                 out*. After injection the text is one user message among
#                 many; the model's later prose, tool calls and final answer
#                 carry no back-reference to it and the loop never asks for
#                 one. A "completed" written at turn end would mean only "the
#                 turn ended", which is precisely the false assurance this
#                 work exists to remove: a reader would take it as evidence
#                 the correction was applied when no such evidence exists.
#                 When a real signal appears (the model quoting a steer id,
#                 say), this is where it goes.
#   superseded    nothing replaces one queued message with another. Sends are
#                 additive and every queued record is either delivered or
#                 cancelled, so the state would never be set and would
#                 advertise a capability the system does not have.
STEER_STATES = ("queued", "acknowledged", "injected", "cancelled", "failed")
# Nothing moves out of these; a message that reaches one is history, not queue.
STEER_TERMINAL = ("injected", "cancelled", "failed")

# Queues are deliberately per (session, run), not merely per session.  A chat
# can have a foreground turn while a detached continuation, worker hand-off,
# or background follow-up is also winding down.  With a session-only key, the
# first loop to reach a round would consume (or the first to finish would
# cancel) a correction intended for the other one.
#
# ``None`` is the legacy/unbound bucket.  It remains for callers that cannot
# identify a run yet, and is never drained by a run-specific loop.
_STEER: Dict[tuple[str, Optional[str]], List[dict]] = {}
_STEER_MAX = 10

# Warning for the two states a human should notice unprompted (a correction is
# waiting; a correction was thrown away), error for the one that means the
# message is definitively not going to be read, info for normal progress.
_STEER_LEVEL = {"queued": "warning", "acknowledged": "info", "injected": "info",
                "cancelled": "warning", "failed": "error"}
_STEER_PHRASE = {"queued": "queued", "acknowledged": "acknowledged by the running turn",
                 "injected": "injected", "cancelled": "cancelled", "failed": "failed"}
# `status` events are what the dashboard reads as an agent's latest step, which
# is right for the two transitions that mean work is happening and wrong for
# the ones that are bookkeeping about a message the loop never used.
_STEER_EVENT_KIND = {"acknowledged": "status", "injected": "status"}
# How far back a lookup reads the feed. One steering message is at most five
# events and the per-session buffer holds MAX_EVENTS_PER_SESSION, so this is
# "the recent past", not "all of history" — the JSONL file is the archive.
_STEER_SCAN = 300


def _new_steer_id() -> str:
    """Short, unique, and readable in a log line beside a run id."""
    return f"steer-{uuid.uuid4().hex[:10]}"


def _steer_label(rec: dict) -> str:
    """"Peer message" or "Steer" — the two things that share this queue."""
    return "Peer message" if str(rec.get("kind") or "user") == "peer" else "Steer"


def _steer_transition(rec: dict, state: str, *, reason: Optional[str] = None,
                      round_num: Optional[int] = None, run_id: Optional[str] = None) -> dict:
    """Record one state change, both on the record and on the activity feed.

    Both halves matter. The record is what the caller and ``pending_steer``
    see right now; the feed entry is all that anyone asking "did that steer
    land?" an hour later has to work from, so it carries the message's whole
    identity — id, state, target, sender, when it was queued, how old it was
    at this transition, which round took it, why it failed — as structured
    ``data`` rather than only a sentence of prose. ``activity.publish``
    swallows its own errors, so a steering message is never lost because
    observing it failed.
    """
    now = time.time()
    queued_at = float(rec.get("queued_at") or rec.get("ts") or now)
    rec["state"] = state
    rec.setdefault("timestamps", {})[state] = now
    if round_num is not None:
        rec["round"] = int(round_num)
    if reason:
        rec["reason"] = str(reason)[:300]
    session_id = str(rec.get("session_id") or "")
    text = str(rec.get("text") or "")
    label = _steer_label(rec)
    data: Dict[str, Any] = {
        "steer_id": rec.get("id"),
        "steer_state": state,
        "steer_kind": rec.get("kind") or "user",
        # The target is the session this event is filed under, but naming it
        # explicitly means a row lifted out of the feed (or read from the
        # global stream) still says which agent was being steered.
        "target_session": session_id or None,
        "target_run": rec.get("run_id") or None,
        "queued_at": queued_at,
        "state_at": now,
        "age_s": round(now - queued_at, 3),
        "text": text[:400],
    }
    for key in ("from_session", "from_session_name"):
        if rec.get(key):
            data[key] = rec[key]
    if round_num is not None:
        data["round"] = int(round_num)
    if rec.get("reason"):
        data["reason"] = rec["reason"]
    phrase = _STEER_PHRASE.get(state, state)
    where = f" (round {int(round_num)})" if round_num is not None and state == "injected" else ""
    why = f" — {rec['reason']}" if rec.get("reason") and state in ("failed", "cancelled") else ""
    title = f"{label} {phrase}{where}{why}: {text[:160]}" if text else f"{label} {phrase}{where}{why}"
    activity.publish(
        session_id, _STEER_EVENT_KIND.get(state, "note"), title, source="odysseus",
        run_id=run_id or activity.active_turn(session_id), owner=rec.get("owner"),
        detail=text or None, data=data, level=_STEER_LEVEL.get(state, "info"),
    )
    return rec


def _steer_key(session_id: str, run_id: Optional[str]) -> tuple[str, Optional[str]]:
    return str(session_id), str(run_id) if run_id else None


def persist_applied_steer(session, event: dict) -> None:
    """Keep delivered instructions in history with their real author identity."""
    from core.models import ChatMessage

    text = str(event.get("text") or "")
    if not text:
        return
    peer = event.get("kind") == "peer"
    metadata = {"source": "agent" if peer else "steer", "steer_id": event.get("steer_id")}
    if peer:
        metadata.update(kind="peer", trusted=False,
                        from_session=event.get("from_session"),
                        from_session_name=event.get("from_session_name"))
    session.add_message(ChatMessage("user", text, metadata))


def _live_run_id(session_id: str) -> Optional[str]:
    """Best-effort live-turn binding without making steering depend on telemetry.

    Normal foreground loops publish an ``odysseus`` active turn.  A headless
    worker has an externally-owned activity record instead, so select that
    record only when it is the *sole* running record for the session.  More
    than one is deliberately ambiguous: an unbound correction is safer than
    silently steering the wrong worker.
    """
    try:
        # The route allocates this before loop preparation, so it covers the
        # only interval where a foreground steer used to become unbound.
        from routes import chat_routes
        stream = (getattr(chat_routes, "_active_streams", {}) or {}).get(str(session_id))
        if isinstance(stream, dict) and str(stream.get("mode") or "").lower() == "agent":
            stream_run = stream.get("steer_run_id")
            if stream_run:
                return str(stream_run)
        # Headless wrapper ids are the queue identity for detached workers;
        # their loop telemetry has a separate odysseus run id.
        from src.headless_agent import steering_run_id, has_steering_runs
        wrapper_run = steering_run_id(session_id)
        if wrapper_run:
            return wrapper_run
        # Do not fall through to whichever telemetry run happened to publish
        # last when two detached wrappers share this session.
        if has_steering_runs(session_id):
            return None
        active = activity.active_turn(session_id)
        if active:
            return active
        running = activity.list_runs(session_id=str(session_id), active_only=True, limit=2)
        if len(running) == 1:
            return str(running[0].get("run_id") or "") or None
    except Exception:
        pass
    return None


def steer(session_id: str, text: str, *, owner: Optional[str] = None, kind: str = "user",
          from_session: Optional[str] = None, from_session_name: Optional[str] = None,
          run_id: Optional[str] = None) -> dict:
    """Queue a message for the next round of ``session_id``'s turn.

    ``kind``/``from_session``/``from_session_name`` are optional and default to
    the original human-steer shape (``kind="user"``, no sender) so every
    existing caller — the Agents dashboard, the chat composer — is unaffected.
    ``agent_mailbox.send()`` is the other caller: it passes ``kind="peer"`` and
    the sending session so a peer message can be told apart from a human's
    steer (see ``pending_steer``/``agent_mailbox.inbox``) even though both live
    in this same queue and drain through the same ``drain_steer``.

    The returned record carries ``id`` and ``state`` as well as ``text``, so a
    caller can hand the id back to whoever sent the message and that message
    can be looked up afterwards (``steer_history``).
    """
    text = " ".join(str(text or "").split())[:4000]
    if not text:
        # Never a queued message, so there is nothing to give a lifecycle to;
        # the caller is told synchronously and shows its own error.
        raise ValueError("steer text is empty")
    # Resolve once at acceptance.  Looking up the active turn later makes a
    # queued correction drift into whichever run happens to be active then.
    target_run = str(run_id) if run_id else _live_run_id(str(session_id))
    now = time.time()
    rec = {"id": _new_steer_id(), "session_id": str(session_id), "text": text, "ts": now,
           "queued_at": now, "owner": owner, "kind": kind, "state": "queued", "timestamps": {},
           "run_id": target_run}
    if from_session:
        rec["from_session"] = from_session
    if from_session_name:
        rec["from_session_name"] = from_session_name
    queue = _STEER.setdefault(_steer_key(str(session_id), target_run), [])
    if len(queue) >= _STEER_MAX:
        # A refusal is exactly the case the operator needs afterwards: the
        # sender believes it steered, and without this nothing would ever
        # mention this message again.
        _steer_transition(rec, "failed", reason=f"queue full ({_STEER_MAX} already waiting)")
        raise ValueError("too many queued steer messages")
    queue.append(rec)
    _steer_transition(rec, "queued")
    return rec


def note_refused(session_id: str, text: str, reason: str, *, owner: Optional[str] = None,
                 kind: str = "user", from_session: Optional[str] = None) -> dict:
    """Record a steering message that was turned away before it was queued.

    The dashboard refuses a steer when the chat is not running, or is running
    something with no rounds to land between (``is_steerable``). The sender
    gets an error, but the *target's* timeline said nothing at all — so the
    only trace of a correction someone tried to make lived in one browser
    toast. This writes it where the rest of the lifecycle lives, as a
    ``failed`` record that was never in the queue.
    """
    text = " ".join(str(text or "").split())[:4000]
    now = time.time()
    rec = {"id": _new_steer_id(), "session_id": str(session_id), "text": text, "ts": now,
           "queued_at": now, "owner": owner, "kind": kind, "state": "failed", "timestamps": {}}
    if from_session:
        rec["from_session"] = from_session
    return _steer_transition(rec, "failed", reason=reason)


def drain_steer(session_id: Optional[str], *, run_id: Optional[str] = None) -> List[str]:
    """Messages queued since the last round, oldest first (and cleared).

    Delegates to ``drain_steer_records`` so a caller that only wants the text
    still moves the messages to ``acknowledged``: a drain that left no trace is
    the hole this whole lifecycle exists to close.
    """
    return [rec["text"] for rec in drain_steer_records(session_id, run_id=run_id)]


def drain_steer_records(session_id: Optional[str], *, run_id: Optional[str] = None,
                        round_num: Optional[int] = None) -> List[dict]:
    """Like ``drain_steer``, but keeps each record's metadata instead of just its text.

    ``drain_steer`` returns bare strings because a caller that only appends
    them needs nothing else. The agent loop calls this one, because the wrapper
    it puts around a drained message depends on ``rec["kind"]`` (``"peer"`` vs
    the default ``"user"``) — labelling a peer agent's message as coming from
    the user changes whose instructions the model thinks it is following — and
    because it has to mark each record ``injected`` once it really has appended
    it.

    Draining is what moves a message to ``acknowledged``: the queue is popped
    here, so from this point the running turn owns the message, and a reader of
    the feed can tell "a live loop took it" apart from "it was still sitting
    there when the turn ended". ``round_num`` is recorded when the caller knows
    it, so the acknowledgement says which round took the message.
    """
    if not session_id:
        return []
    queue = _STEER.pop(_steer_key(str(session_id), run_id), None)
    if not queue:
        return []
    for rec in queue:
        _steer_transition(rec, "acknowledged", round_num=round_num)
    return list(queue)


def mark_injected(rec: dict, *, round_num: Optional[int] = None,
                  run_id: Optional[str] = None) -> dict:
    """The loop really did append this message to the model's messages.

    Called from the drain site in ``agent_loop.py`` *after* the append, not
    before: the point of a separate state is that "the loop took it" and "the
    model was given it" are different claims, and the gap between them is
    where a drained message can still be dropped.
    """
    return _steer_transition(rec, "injected", round_num=round_num, run_id=run_id)


def mark_failed(rec: dict, reason: str, *, round_num: Optional[int] = None,
                run_id: Optional[str] = None) -> dict:
    """A drained message that could not be injected, with why."""
    return _steer_transition(rec, "failed", reason=reason, round_num=round_num, run_id=run_id)


def pending_steer(session_id: str, *, run_id: Optional[str] = None) -> List[dict]:
    """Records still waiting to be drained — the live queue, nothing else.

    This still backs the ``steer_queued`` count on the Agents overview, and the
    number means more than it did, not less: a message leaves the queue the
    moment a turn acknowledges it, so a non-zero count is now exactly "this
    many corrections no turn has picked up yet". What used to vanish from the
    count without trace is now an ``acknowledged``/``injected`` record on the
    feed, so the count dropping to zero can be read against what became of each
    message instead of being the end of the story.
    """
    if run_id is not None:
        return list(_STEER.get(_steer_key(session_id, run_id), ()))
    # Public overview callers historically ask by session.  Keep that useful
    # aggregate while run loops must opt into their exact bucket above.
    sid = str(session_id)
    return [rec for (queued_session, _), queue in _STEER.items() if queued_session == sid for rec in queue]


def clear_steer_records(session_id: Optional[str], *,
                        run_id: Optional[str] = None) -> List[dict]:
    """Drop anything still queued for a turn that has ended, returning it.

    The queue is keyed by session and run, so a steer nobody drained would sit
    in that run's bucket until an unsafe later adoption. The agent loop extends
    a turn to absorb a late steer (see the round loop), so reaching here means
    that run really is over — each dropped message is marked ``cancelled`` with
    that reason, so "it never landed" is on the record instead of only in a
    server log line the operator never sees.

    Records rather than bare strings, mirroring ``drain_steer_records``: the
    client has to settle the pending chip for THIS message and hand its text
    back to the user, and it can only do either if the id comes with it.
    """
    if not session_id:
        return []
    queue = _STEER.pop(_steer_key(str(session_id), run_id), None)
    if not queue:
        return []
    for rec in queue:
        _steer_transition(rec, "cancelled", reason="the turn ended before it was drained")
    return list(queue)


def clear_steer(session_id: Optional[str], *, run_id: Optional[str] = None) -> List[str]:
    """``clear_steer_records`` for callers that only need the text."""
    return [rec["text"] for rec in clear_steer_records(session_id, run_id=run_id)]


def steer_history(session_id: str, *, limit: int = 20) -> List[dict]:
    """Reconstruct each recent steering message's lifecycle from the feed.

    One message is several events; this folds them back into one row per id —
    ``state`` is the newest one seen, ``timestamps`` maps every state to when
    it happened — so a caller gets "what became of it" rather than a pile of
    transitions to reassemble. Reading the feed instead of a cache is the point
    of keeping the history there: it works for a session whose queue was
    drained long ago, and after a restart that emptied ``_STEER`` entirely.
    """
    rows: Dict[str, dict] = {}
    try:
        events = activity.history(str(session_id or ""), limit=_STEER_SCAN)
    except Exception:  # a broken feed must not break the dashboard
        logger.debug("steer_history: feed read failed for %s", session_id, exc_info=True)
        events = []
    for ev in events:
        data = ev.get("data")
        if not isinstance(data, dict) or not data.get("steer_id"):
            continue
        sid = str(data["steer_id"])
        row = rows.setdefault(sid, {"id": sid, "session_id": ev.get("session_id"),
                                    "kind": "user", "text": "", "state": None,
                                    "timestamps": {}, "queued_at": None})
        state = str(data.get("steer_state") or "")
        if state:
            # Events replay oldest first, so the last one wins — which is the
            # message's current state even if a transition was published late.
            row["state"] = state
            row["timestamps"][state] = data.get("state_at") or ev.get("ts")
        for key in ("steer_kind", "queued_at", "text", "reason", "round", "from_session",
                    "from_session_name", "target_session", "target_run"):
            if data.get(key) not in (None, ""):
                row["kind" if key == "steer_kind" else key] = data[key]
        row["updated_at"] = ev.get("ts")
    ordered = sorted(rows.values(), key=lambda r: r.get("queued_at") or r.get("updated_at") or 0,
                     reverse=True)
    return ordered[: max(1, int(limit or 20))]


def steer_status(session_id: str, *, limit: int = 6) -> dict:
    """What the Control Room shows about steering for one chat.

    ``queued`` is the live count (the same number ``steer_queued`` has always
    been); ``messages`` is the recent lifecycle, newest first. A row still in
    state ``queued`` is cross-checked against the live queue and marked
    ``live: False`` when it is not in it, because the queue is in memory only:
    a message queued before a restart is not waiting for anything, and saying
    so is the difference between "still pending" and "silently lost" — the
    exact confusion this is meant to end.
    """
    live = {str(rec.get("id")) for rec in pending_steer(session_id)}
    messages = steer_history(session_id, limit=limit)
    for row in messages:
        if row.get("state") == "queued":
            row["live"] = row["id"] in live
    return {"queued": len(live), "messages": messages}


def is_steerable(session_id: str) -> bool:
    """True unless we know the running turn has no rounds to land between.

    Only the agent loop drains the queue, and only between rounds. Everything
    that runs through it is steerable — chat agent turns, but equally the
    detached worker / background-job / pipeline runs tracked via
    ``agent_runs.track_external``, which have no ``_active_streams`` entry at
    all. The one case to refuse is a plain single-shot chat reply: it is "busy"
    but never reaches the loop, so a steer would sit in the queue unread and
    then surface inside some unrelated later turn.

    So this reads as "reject what is positively known to be non-agent", not
    "accept only what is positively known to be an agent turn" — the latter
    silently broke steering for workers.
    """
    try:
        from routes import chat_routes

        streams = getattr(chat_routes, "_active_streams", {}) or {}
    except Exception:
        return False
    rec = streams.get(str(session_id))
    if isinstance(rec, dict) and "mode" in rec:
        return (str(rec.get("mode") or "").strip().lower() == "agent"
                and _live_run_id(str(session_id)) is not None)
    # Detached work is steerable only when its wrapper identity is known. Do
    # not accept an unbound correction that no named loop can drain.
    return _live_run_id(str(session_id)) is not None


# ── stop ──────────────────────────────────────────────────────────────────

async def stop_run(run_id: str) -> dict:
    """Stop one run. Returns ``{"stopped": bool, "how": ..., ...}``; raises
    ``LookupError`` when unknown and ``ValueError`` when not stoppable."""
    rec = activity.get_run(run_id)
    if rec is None:
        raise LookupError("Run not found")
    if rec.get("status") != "running":
        return {"stopped": False, "status": rec.get("status"), "reason": "not running"}
    summary = rec.get("summary") or {}
    source = rec.get("source")
    if summary.get("workflow_controller"):
        from src import agent_workflows
        result = await agent_workflows.inspect(workflow_id=run_id, session_id=rec["session_id"],
                                                owner=rec.get("owner"), action="cancel")
        return {"stopped": result["status"] == "cancelled", "how": "workflow", "status": result["status"]}
    from src.headless_agent import request_stop

    if request_stop(run_id):
        return {"stopped": True, "how": "headless"}
    if source == "claude_code":
        from src.agent_tools.claude_code_tools import get_task_runner

        record = await get_task_runner().cancel(summary.get("task_id") or run_id)
        if record is None:
            raise ValueError("This Claude Code run is part of a chat turn; stop the chat to stop it")
        return {"stopped": True, "how": "claude_code", "status": record.get("status")}
    if source == "bg_job" and summary.get("job_id"):
        from src import bg_jobs

        job = bg_jobs.kill(str(summary["job_id"]))
        if job is None:
            raise LookupError("Background job not found")
        return {"stopped": True, "how": "bg_job", "status": job.get("status")}
    if source == "odysseus" and rec.get("session_id"):
        from src import agent_runs

        return {"stopped": agent_runs.stop(rec["session_id"]), "how": "chat"}
    raise ValueError(f"{source or 'This'} runs can't be stopped individually")


# ── wrap up (soft stop) ───────────────────────────────────────────────────

WRAP_UP_TEXT = ("Wrap up now: stop starting new work, and within the next one or two rounds "
                "return your result from what you already have, noting anything left unfinished.")


def wrap_up(run_id: str, *, owner: Optional[str] = None) -> dict:
    """Ask a live agent run to finish with what it has, instead of killing it.

    Stop cancels the drain and hands back whatever text happened to exist; a
    long worker is usually mid-tool at that point, so the parent gets partial
    work with no summary. This queues an ordinary steer, bound to the run the
    way the loop drains it, so the model itself writes the hand-back. Raises
    ``LookupError`` when unknown and ``ValueError`` when the run has no agent
    loop that would read the message.
    """
    rec = activity.get_run(run_id)
    if rec is None:
        raise LookupError("Run not found")
    if rec.get("status") not in activity.LIVE_RUN_STATUSES:
        raise ValueError("This run is not running")
    session_id = str(rec.get("session_id") or "")
    from src.headless_agent import serves_run

    if serves_run(session_id, run_id):
        # A detached worker drains exactly its wrapper run's queue.
        target = run_id
    elif (rec.get("source") == "odysseus" and activity.active_turn(session_id) == run_id
          and is_steerable(session_id)):
        # A chat turn drains the queue its stream allocated, which is not the
        # activity run id; let `steer` resolve it from the live stream.
        target = None
    else:
        raise ValueError("This run has no agent rounds to deliver a wrap-up to; stop it instead")
    return steer(session_id, WRAP_UP_TEXT, owner=owner or rec.get("owner"), run_id=target)


# ── launch a worker ───────────────────────────────────────────────────────

_WORKERS: Dict[str, asyncio.Task] = {}


async def launch_worker(*, owner: Optional[str], task: str, profile_name: Optional[str] = None,
                        parent_session: Optional[str] = None, model: Optional[str] = None,
                        inline_profile: Optional[dict] = None, handoff: bool = True,
                        run_metadata: Optional[dict] = None, runtime_settings: Optional[dict] = None,
                        workspace: Optional[str] = None, requires: Optional[List[str]] = None,
                        preflight: bool = True) -> dict:
    """Start a worker in a fresh chat and return at once with its ids.

    The worker runs detached (like a chat turn survives a closed tab); its
    progress is on its own chat's activity feed and on the parent's when one
    is given, so the dashboard and the parent chat both see it.

    Unless ``preflight`` is off, src.worker_preflight checks the task first:
    it binds a workspace (``workspace``, else the parent chat's, else a
    checkout the task names) and attaches the file tools the task needs, or
    raises WorkerBlocked (a ValueError) with the fix, before any chat exists.
    """
    from src import agent_profiles, agent_runs
    from src.agent_tools.session_tools import _new_child_session
    from src.ai_interaction import get_session_manager
    from src.headless_agent import run_headless

    task = str(task or "").strip()
    if not task:
        raise ValueError("task is empty")
    manager = get_session_manager()
    if manager is None:
        raise RuntimeError("session manager unavailable")
    if inline_profile is not None and (profile_name or model):
        raise ValueError("inline_profile cannot be combined with profile_name or model")
    profile = agent_profiles.validate_profiles([inline_profile])[0] if inline_profile is not None else None
    if profile_name:
        profile = agent_profiles.get_profile(profile_name)
        if profile is None:
            raise ValueError(f"no agent profile named {profile_name!r}")
    if model:
        profile = dict(profile or {"name": "worker", "instructions": "", "disabled_tools": [],
                                   "max_rounds": agent_profiles.DEFAULT_ROUNDS})
        profile["model"] = model
    checked = None
    if preflight:
        from src import worker_preflight
        from src.agent_tools.session_tools import _caller_workspace

        checked = worker_preflight.run_preflight(
            task,
            explicit_workspace=workspace,
            inherited_workspace=_caller_workspace(parent_session),
            unavailable_tools=worker_preflight.worker_unavailable_tools(owner, profile),
            requires=requires or (),
        )
        if not checked.ok:
            worker_preflight.record_blocked(parent_session, owner, task, checked)
            raise worker_preflight.WorkerBlocked(checked)
    child_kwargs = {"workspace": checked.workspace} if checked and checked.workspace else {}
    sess, err = _new_child_session(manager, parent_session, owner, task, profile, **child_kwargs)
    if err:
        raise ValueError(err)
    if inline_profile is not None:
        # Workflow profiles are intentionally transient. Persist their complete
        # narrowed policy on the child before starting; the legacy child helper
        # is best-effort, which must not turn a failed save into elevated access.
        from core.database import update_session_settings
        patch = {**agent_profiles.session_patch(profile), **(runtime_settings or {})}
        if parent_session:
            patch["parent_session"] = parent_session
        if update_session_settings(sess.id, patch) is None:
            raise RuntimeError("Could not persist the worker's scoped policy; worker not started")
    try:
        manager.save_sessions()
    except Exception:
        pass
    # The persona is a persisted per-session snapshot consumed by agent_loop
    # on this first turn and every reopened turn. Keep it out of history so it
    # cannot be duplicated or drift from the saved agent configuration.
    context: List[Dict[str, Any]] = [{"role": "user", "content": task}]
    # Show the task in the worker's chat from the start. It was saved only when
    # the run ended, so opening a running worker's chat showed an empty "New
    # chat ready" screen. The run reads `context`, not the chat history.
    try:
        from core.models import ChatMessage as _ChatMessage
        sess.add_message(_ChatMessage("user", task, {"source": "dashboard", "direction": "inbound"}))
        manager.save_sessions()
    except Exception:
        logger.debug("worker task persist failed", exc_info=True)
    label = f"{profile['name']} · " if profile and profile.get("name") not in (None, "worker") else ""
    # The loadout's own round budget, or 0 for no ceiling (the default). It is
    # recorded on the run and returned to the caller because it is the number a
    # "ran out of rounds" result has to be read against — otherwise a cap that
    # ended a worker is invisible until someone reopens the loadout in Settings.
    rounds = int(profile["max_rounds"] if profile else agent_profiles.DEFAULT_ROUNDS)
    # A saved loadout's positive budget is one its author chose, so at that
    # round the worker is asked to wrap up and hand back what it has. A
    # workflow's inline profile carries a default (12) nobody picked, and a
    # model-only worker has none; both stay advisory.
    wrap_up_round = rounds if profile_name and rounds > 0 else 0
    run_id = activity.run_started(
        sess.id, "session", f"Worker · {label}{task[:80]}", owner=owner,
        data={"target_session": sess.id, "target_session_name": sess.name, "model": sess.model,
              "mode": "agent", "launched_from": "dashboard", "max_rounds": rounds,
              **({"profile": profile["name"]} if profile and profile.get("name") else {}),
              **({"parent_session": parent_session} if parent_session else {}),
              **(run_metadata or {})},
        detail=task[:1500],
    )
    if parent_session:
        # The worker chat's name already carries the loadout and the task
        # ("↳ Scout: Write a short poem…"); the full task is in `detail`.
        activity.publish(parent_session, "message", f"→ worker {sess.name}", source="session",
                         run_id=run_id, owner=owner, detail=task[:2000])

    async def _run():
        from core.models import ChatMessage

        outcome: Dict[str, Any] = {}
        status, text, events, error_detail = "completed", "", [], ""
        try:
            with agent_runs.track_external(sess.id, source="worker", owner=owner):
                text, events = await run_headless(
                    sess, list(context),
                    max_rounds=rounds,
                    disabled_tools=set(profile.get("disabled_tools") or []) if profile else frozenset(),
                    activity_session_id=sess.id, run_id=run_id, source="session", owner=owner,
                    outcome=outcome,
                    workspace=checked.workspace if checked else None,
                    forced_tools=checked.forced_tools if checked else None,
                    wrap_up_round=wrap_up_round,
                )
            if outcome.get("stopped"):
                status = "cancelled"
            elif outcome.get("awaiting_approval"):
                # Ended on an approval card in the worker's chat: paused, not done.
                status = "waiting_approval"
            elif outcome.get("rounds_exhausted"):
                # Every leg was spent and it is still not done. Reporting that
                # as "completed" is what let a cut-off worker hand the parent an
                # empty result that read like a finished one; `run_headless` has
                # already appended the "here is where I got to" line to `text`.
                status = "incomplete"
        except asyncio.CancelledError:
            status = "cancelled"
        except Exception as exc:
            status, text = "failed", f"Worker failed: {exc}"
            error_detail = str(exc)[:2000]
            logger.warning("worker %s failed: %s", sess.id, exc, exc_info=True)
        try:
            meta: Dict[str, Any] = {"source": "worker", "model": sess.model, "run_id": run_id,
                                    "status": status, **(run_metadata or {})}
            if error_detail:
                meta["error"] = error_detail
            if events:
                meta["tool_events"] = events
            sess.add_message(ChatMessage("assistant", text or "(no reply)", meta))
            manager.save_sessions()
        except Exception:
            logger.debug("worker persist failed", exc_info=True)
        exhausted = bool(outcome.get("rounds_exhausted"))
        total_rounds = rounds or 0  # 0 = unlimited
        activity.run_finished(sess.id, "session", run_id,
                              f"Worker · {label}{sess.name} {status}", status=status, owner=owner,
                              data={"target_session": sess.id, "steps": len(events), "result_excerpt": text[:400],
                                    "max_rounds": total_rounds, "rounds_exhausted": exhausted,
                                    **({"error": error_detail} if error_detail else {})})
        if parent_session:
            # Two events, because they answer different questions. The message
            # carries the result text; the status event is what closes the run
            # in the chat that STARTED this worker. Without it the parent's
            # agent strip kept a row at "running" forever and then, on the next
            # reconcile, decided the run it could not find had been interrupted
            # -- which is why sub-agents stopped appearing above the composer.
            cut = ((f" (stopped after {total_rounds} rounds with work outstanding)" if total_rounds
                    else " (stopped with work outstanding)") if exhausted else "")
            activity.publish(parent_session, "message", f"← worker {sess.name}: {text[:160]}", source="session",
                             run_id=run_id, owner=owner, detail=text[:2000],
                             level="error" if status == "failed" else "info")
            activity.publish(parent_session, "status", f"Worker {sess.name} {status}{cut}", source="session",
                             run_id=run_id, owner=owner,
                             level="error" if status == "failed" else "info",
                             data={"status": status, "target_session": sess.id, "parent_session": parent_session,
                                   "max_rounds": total_rounds, "rounds_exhausted": exhausted,
                                   **({"error": error_detail} if error_detail else {})})
            if handoff:
                try:
                    await _hand_off(manager, parent_session, sess, task, text, status, owner)
                except Exception:
                    logger.warning("worker hand-off to %s failed", parent_session, exc_info=True)
        _WORKERS.pop(run_id, None)

    worker_task = asyncio.create_task(_run())
    _WORKERS[run_id] = worker_task

    def _cleanup_worker(done):
        # Cancelling a Task before its coroutine's first instruction bypasses
        # every try/finally inside that coroutine. Always release its registry
        # slot, and close a run that otherwise remains 'running' indefinitely.
        if _WORKERS.get(run_id) is done:
            _WORKERS.pop(run_id, None)
        failure = None if done.cancelled() else done.exception()
        if not done.cancelled() and failure is None:
            return
        try:
            record = activity.get_run(run_id)
            if record and record.get("status") != "running":
                return
            terminal = "cancelled" if done.cancelled() else "failed"
            from core.models import ChatMessage
            sess.add_message(ChatMessage("assistant", "", {
                "source": "worker", "model": sess.model, "run_id": run_id,
                "status": terminal, **(run_metadata or {}),
            }))
            manager.save_sessions()
        except Exception:
            logger.debug("worker task cleanup could not persist result", exc_info=True)
        finally:
            try:
                # Do not downgrade a worker that completed before cancellation
                # interrupted its optional parent continuation.
                record = activity.get_run(run_id)
                if record is None or record.get("status") == "running":
                    terminal = "cancelled" if done.cancelled() else "failed"
                    activity.run_finished(sess.id, "session", run_id, "Worker task ended before completion",
                                          status=terminal, owner=owner,
                                          data={"target_session": sess.id})
                    if parent_session:
                        # Same reason as the normal completion path: close the
                        # row in the chat that started this worker.
                        activity.publish(parent_session, "status",
                                         f"Worker {sess.name} {terminal} before completion", source="session",
                                         run_id=run_id, owner=owner, level="error",
                                         data={"status": terminal, "target_session": sess.id,
                                               "parent_session": parent_session})
            except Exception:
                logger.debug("worker task cleanup could not close activity run", exc_info=True)

    worker_task.add_done_callback(_cleanup_worker)
    return {"session_id": sess.id, "session_name": sess.name, "run_id": run_id, "model": sess.model,
            "max_rounds": rounds, **({"preflight": checked.summary()} if checked else {})}


def collect_worker_result(run_id: str, *, owner: Optional[str], session_id: Optional[str] = None) -> dict:
    """Read actual persisted worker output, never infer completion from prose."""
    from src.ai_interaction import get_session_manager

    rec = activity.get_run(run_id)
    if rec and (rec.get("owner") != owner or (session_id and rec.get("session_id") != session_id)):
        raise LookupError("Worker run not found")
    if rec is None and not session_id:
        raise LookupError("Worker run not found")
    manager = get_session_manager()
    sess = manager.get_session(rec["session_id"] if rec else session_id) if manager else None
    if sess is None or getattr(sess, "owner", None) != owner:
        raise LookupError("Worker chat not found")
    result = {"run_id": run_id, "session_id": sess.id, "status": rec.get("status") if rec else "unknown",
              "result": "", "tool_calls": []}
    for message in reversed(getattr(sess, "history", [])):
        meta = message.get("metadata") or {}
        if message.get("role") == "assistant" and meta.get("run_id") == run_id:
            if rec is None:
                # The small dashboard run registry may rotate long before the
                # durable child chat. Only an exact message run-id match can
                # recover its artifact and terminal status after eviction.
                result["status"] = meta.get("status") or "unknown"
            if meta.get("error"):
                result["error"] = str(meta["error"])[:2000]
            text = str(message.get("content") or "")
            result["result"] = "" if text == "(no reply)" else text[:20000]
            result["result_truncated"] = len(text) > 20000
            result["tool_calls"] = [
                {"tool": ev.get("tool"), "exit_code": ev.get("exit_code"), "round": ev.get("round"),
                 **({"error": ev["error"]} if ev.get("error") else {})}
                for ev in (meta.get("tool_events") or [])[:120] if isinstance(ev, dict)
            ]
            break
    else:
        if rec is None:
            raise LookupError("Worker result not found in the specified chat")
    return result


_HANDOFF_MAX_ROUNDS = 12
# A worker that finishes while its parent chat is mid-turn waits for that turn
# to end, then the parent continues with the result. Bounded, so a chat that
# never goes idle does not hold a task open forever.
_HANDOFF_IDLE_POLL_S = 2.0
_HANDOFF_IDLE_WAIT_S = 30 * 60
_PENDING_HANDOFFS: set = set()


async def _hand_off(manager, parent_id: str, worker, task: str, text: str, status: str,
                    owner: Optional[str]) -> None:
    """Deliver a finished worker's result to the chat it reports to.

    The result is saved into the parent chat as a message from the worker,
    so the parent's next turn has it. If the parent is idle, its agent
    continues right away (like a background job's follow-up) so a plan that
    delegated a piece of work picks the piece up without the user relaying it.
    """
    from core.models import ChatMessage
    from src import agent_runs
    from src.headless_agent import run_headless

    parent = manager.get_session(parent_id)
    if parent is None:
        return
    headline = {"completed": "finished", "incomplete": "ran out of rounds",
                "waiting_approval": "is waiting for the user's approval"}.get(status, status)
    inject = (f"[Worker {worker.name} {headline}]\nTask: {task[:1500]}\n\nResult:\n{text[:12000]}\n\n"
              + ("The worker was cut off by its round budget, so the result above is partial — "
                 "pick the task up from where it stopped. " if status == "incomplete" else "")
              + ("The worker paused on an approval card in its own chat, so the task is not done. "
                 "Tell the user it needs their approval there; do not redo the work. "
                 if status == "waiting_approval" else "")
              + "Continue the task using this result. Don't repeat work the worker already did. "
              "If the task is now complete, give the user the final result.")
    inject_msg = ChatMessage("user", inject, {"source": "worker", "from_session": worker.id,
                                              "from_session_name": worker.name, "direction": "inbound"})
    parent.add_message(inject_msg)
    manager.save_sessions()
    if agent_runs.is_busy(parent_id):
        # Mid-turn. That turn built its context before this message existed,
        # so it will not read the result. Previously the result just sat in
        # history until the user happened to send something, and the worker
        # looked like it had produced nothing. Continue once the turn ends.
        activity.publish(parent_id, "note",
                         f"Worker {worker.name} finished; this chat continues with its result when the current turn ends",
                         source="session", owner=owner)
        task = asyncio.create_task(_continue_when_idle(manager, parent_id, parent, worker, inject_msg, owner))
        _PENDING_HANDOFFS.add(task)
        task.add_done_callback(_PENDING_HANDOFFS.discard)
        return
    await _continue_parent(manager, parent_id, parent, worker, owner)


def _result_already_read(parent, inject_msg) -> bool:
    """Whether a turn after the hand-off already had the result in context:
    a message the user (not a worker) sent after it started that turn."""
    history = list(getattr(parent, "history", None) or [])
    try:
        start = next(i for i, m in enumerate(history) if m is inject_msg)
    except StopIteration:
        return False
    for m in history[start + 1:]:
        meta = getattr(m, "metadata", None) or {}
        if getattr(m, "role", None) == "user" and meta.get("source") != "worker":
            return True
    return False


async def _continue_when_idle(manager, parent_id: str, parent, worker, inject_msg, owner: Optional[str]) -> None:
    from src import agent_runs

    deadline = time.monotonic() + _HANDOFF_IDLE_WAIT_S
    try:
        while agent_runs.is_busy(parent_id):
            if time.monotonic() > deadline:
                activity.publish(parent_id, "note",
                                 f"Worker {worker.name}'s result is waiting in this chat; it was busy too long to continue automatically",
                                 source="session", owner=owner)
                return
            await asyncio.sleep(_HANDOFF_IDLE_POLL_S)
        if _result_already_read(parent, inject_msg):
            return
        await _continue_parent(manager, parent_id, parent, worker, owner)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("deferred worker hand-off to %s failed", parent_id, exc_info=True)


async def _continue_parent(manager, parent_id: str, parent, worker, owner: Optional[str]) -> None:
    """Run the parent chat's agent on the worker's result (it is already in
    the parent's history) and save its reply there."""
    from core.models import ChatMessage
    from src import agent_runs
    from src.headless_agent import run_headless

    run_id = activity.run_started(parent_id, "session", f"Continuing after worker {worker.name}", owner=owner,
                                  data={"target_session": worker.id, "target_session_name": worker.name,
                                        "mode": "agent"})
    reply, events = "", []
    # The continuation is itself a bounded agent run, so it can be cut off the
    # same way the worker was — report that rather than closing the run green.
    followup: Dict[str, Any] = {}
    try:
        with agent_runs.track_external(parent_id, source="worker", owner=owner):
            reply, events = await run_headless(
                parent, parent.get_context_messages(), max_rounds=_HANDOFF_MAX_ROUNDS,
                # Not a sub-agent: this is the parent's own chat continuing
                # itself, so `run_headless` runs it under that chat's own
                # stored policy — the tools it has switched off and its
                # approval mode. A chat does not lose its own restrictions
                # because a worker happened to finish.
                subagent=False,
                activity_session_id=parent_id, run_id=run_id,
                source="session", owner=owner, outcome=followup)
        status = "incomplete" if followup.get("rounds_exhausted") else "completed"
    except Exception as exc:
        reply, status = f"Could not continue after the worker: {exc}", "failed"
    meta: Dict[str, Any] = {"model": parent.model, "source": "worker_followup", "worker_session": worker.id}
    if events:
        meta["tool_events"] = events
    parent.add_message(ChatMessage("assistant", reply or "(no reply)", meta))
    manager.save_sessions()
    activity.run_finished(parent_id, "session", run_id, f"Continued after worker {worker.name}", status=status,
                          owner=owner, data={"target_session": worker.id, "steps": len(events),
                                             "result_excerpt": reply[:400]})
