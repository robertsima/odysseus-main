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


def live_children(session_id: Optional[str]) -> int:
    """Child runs currently in flight for ``session_id``.

    The per-chat ``max_parallel_workers`` limit is checked in two places — the
    spawning-tool gate in :mod:`src.tool_execution` and the loadout tool's
    ``start`` action — so the counting rule lives here rather than being
    written twice and drifting. ``odysseus`` runs are the chat's own turns, not
    children, so they do not count against the limit.
    """
    if not session_id:
        return 0
    try:
        return sum(
            1 for rec in activity.list_runs(limit=400)
            if rec.get("session_id") == session_id
            and rec.get("status") == "running"
            and rec.get("source") != "odysseus"
        )
    except Exception:
        return 0


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

_STEER: Dict[str, List[dict]] = {}
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


def steer(session_id: str, text: str, *, owner: Optional[str] = None, kind: str = "user",
          from_session: Optional[str] = None, from_session_name: Optional[str] = None) -> dict:
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
    now = time.time()
    rec = {"id": _new_steer_id(), "session_id": str(session_id), "text": text, "ts": now,
           "queued_at": now, "owner": owner, "kind": kind, "state": "queued", "timestamps": {}}
    if from_session:
        rec["from_session"] = from_session
    if from_session_name:
        rec["from_session_name"] = from_session_name
    queue = _STEER.setdefault(str(session_id), [])
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


def drain_steer(session_id: Optional[str]) -> List[str]:
    """Messages queued since the last round, oldest first (and cleared).

    Delegates to ``drain_steer_records`` so a caller that only wants the text
    still moves the messages to ``acknowledged``: a drain that left no trace is
    the hole this whole lifecycle exists to close.
    """
    return [rec["text"] for rec in drain_steer_records(session_id)]


def drain_steer_records(session_id: Optional[str], *, round_num: Optional[int] = None) -> List[dict]:
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
    queue = _STEER.pop(str(session_id), None)
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


def pending_steer(session_id: str) -> List[dict]:
    """Records still waiting to be drained — the live queue, nothing else.

    This still backs the ``steer_queued`` count on the Agents overview, and the
    number means more than it did, not less: a message leaves the queue the
    moment a turn acknowledges it, so a non-zero count is now exactly "this
    many corrections no turn has picked up yet". What used to vanish from the
    count without trace is now an ``acknowledged``/``injected`` record on the
    feed, so the count dropping to zero can be read against what became of each
    message instead of being the end of the story.
    """
    return list(_STEER.get(str(session_id), ()))


def clear_steer(session_id: Optional[str]) -> List[str]:
    """Drop anything still queued for a turn that has ended, returning it.

    The queue is keyed only by session, so a steer nobody drained would sit
    there until some *future* turn picked it up and answered a correction from
    an hour ago with no idea what it referred to. The agent loop extends a turn
    to absorb a late steer (see the round loop), so reaching here means the turn
    really is over — each dropped message is marked ``cancelled`` with that
    reason, so "it never landed" is on the record instead of only in a server
    log line the operator never sees.
    """
    if not session_id:
        return []
    queue = _STEER.pop(str(session_id), None)
    if not queue:
        return []
    for rec in queue:
        _steer_transition(rec, "cancelled", reason="the turn ended before it was drained")
    return [rec["text"] for rec in queue]


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
                    "from_session_name", "target_session"):
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
        return True
    rec = streams.get(str(session_id))
    if not isinstance(rec, dict) or "mode" not in rec:
        return True
    return str(rec.get("mode") or "").strip().lower() == "agent"


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


# ── launch a worker ───────────────────────────────────────────────────────

_WORKERS: Dict[str, asyncio.Task] = {}


async def launch_worker(*, owner: Optional[str], task: str, profile_name: Optional[str] = None,
                        parent_session: Optional[str] = None, model: Optional[str] = None) -> dict:
    """Start a worker in a fresh chat and return at once with its ids.

    The worker runs detached (like a chat turn survives a closed tab); its
    progress is on its own chat's activity feed and on the parent's when one
    is given, so the dashboard and the parent chat both see it.
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
    profile = None
    if profile_name:
        profile = agent_profiles.get_profile(profile_name)
        if profile is None:
            raise ValueError(f"no agent profile named {profile_name!r}")
    if model:
        profile = dict(profile or {"name": "worker", "instructions": "", "disabled_tools": [],
                                   "max_rounds": agent_profiles.DEFAULT_ROUNDS})
        profile["model"] = model
    sess, err = _new_child_session(manager, parent_session, owner, task, profile)
    if err:
        raise ValueError(err)
    try:
        manager.save_sessions()
    except Exception:
        pass
    context: List[Dict[str, Any]] = []
    if profile and profile.get("instructions"):
        context.append({"role": "system", "content": profile["instructions"]})
    context.append({"role": "user", "content": task})
    label = f"{profile['name']} · " if profile and profile.get("name") not in (None, "worker") else ""
    # The loadout's own round budget (validate_profiles clamps it to 1..40, and
    # defaults it to DEFAULT_ROUNDS). Recorded on the run and returned to the
    # caller because it is the number a "ran out of rounds" result has to be read
    # against — otherwise the cap that ended a worker is invisible until someone
    # reopens the loadout in Settings.
    rounds = int(profile["max_rounds"] if profile else agent_profiles.DEFAULT_ROUNDS)
    run_id = activity.run_started(
        sess.id, "session", f"Worker · {label}{task[:80]}", owner=owner,
        data={"target_session": sess.id, "target_session_name": sess.name, "model": sess.model,
              "mode": "agent", "launched_from": "dashboard", "max_rounds": rounds,
              **({"profile": profile["name"]} if profile and profile.get("name") else {})},
        detail=task[:1500],
    )
    if parent_session:
        activity.publish(parent_session, "message", f"→ worker {sess.name}: {task[:160]}", source="session",
                         run_id=run_id, owner=owner, detail=task[:2000])

    async def _run():
        from core.models import ChatMessage

        outcome: Dict[str, Any] = {}
        status, text, events = "completed", "", []
        try:
            with agent_runs.track_external(sess.id, source="worker", owner=owner):
                text, events = await run_headless(
                    sess, context,
                    max_rounds=rounds,
                    disabled_tools=set(profile.get("disabled_tools") or []) if profile else frozenset(),
                    activity_session_id=sess.id, run_id=run_id, source="session", owner=owner, outcome=outcome,
                )
            if outcome.get("stopped"):
                status = "cancelled"
            elif outcome.get("rounds_exhausted"):
                # It did real work and then ran out of rounds. Reporting that as
                # "completed" is what let a cut-off worker hand the parent an
                # empty result that read like a finished one; `run_headless` has
                # already appended the "here is where I got to" line to `text`.
                status = "incomplete"
        except asyncio.CancelledError:
            status = "cancelled"
        except Exception as exc:
            status, text = "failed", f"Worker failed: {exc}"
            logger.warning("worker %s failed: %s", sess.id, exc, exc_info=True)
        try:
            sess.add_message(ChatMessage("user", task, {"source": "dashboard", "direction": "inbound"}))
            meta: Dict[str, Any] = {"source": "worker", "model": sess.model}
            if events:
                meta["tool_events"] = events
            sess.add_message(ChatMessage("assistant", text or "(no reply)", meta))
            manager.save_sessions()
        except Exception:
            logger.debug("worker persist failed", exc_info=True)
        activity.run_finished(sess.id, "session", run_id,
                              f"Worker · {label}{sess.name} {status}", status=status, owner=owner,
                              data={"target_session": sess.id, "steps": len(events), "result_excerpt": text[:400]})
        if parent_session:
            activity.publish(parent_session, "message", f"← worker {sess.name}: {text[:160]}", source="session",
                             run_id=run_id, owner=owner, detail=text[:2000],
                             level="error" if status == "failed" else "info")
            try:
                await _hand_off(manager, parent_session, sess, task, text, status, owner)
            except Exception:
                logger.warning("worker hand-off to %s failed", parent_session, exc_info=True)
        _WORKERS.pop(run_id, None)

    _WORKERS[run_id] = asyncio.create_task(_run())
    return {"session_id": sess.id, "session_name": sess.name, "run_id": run_id, "model": sess.model,
            "max_rounds": rounds}


_HANDOFF_MAX_ROUNDS = 12


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
    headline = {"completed": "finished", "incomplete": "ran out of rounds"}.get(status, status)
    inject = (f"[Worker {worker.name} {headline}]\nTask: {task[:1500]}\n\nResult:\n{text[:12000]}\n\n"
              + ("The worker was cut off by its round budget, so the result above is partial — "
                 "pick the task up from where it stopped. " if status == "incomplete" else "")
              + "Continue the task using this result. Don't repeat work the worker already did. "
              "If the task is now complete, give the user the final result.")
    parent.add_message(ChatMessage("user", inject, {"source": "worker", "from_session": worker.id,
                                                    "from_session_name": worker.name, "direction": "inbound"}))
    manager.save_sessions()
    if agent_runs.is_busy(parent_id):
        # Mid-turn: the message waits in history for the next turn.
        activity.publish(parent_id, "note", f"Worker result saved for the next turn: {worker.name}",
                         source="session", owner=owner)
        return
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
