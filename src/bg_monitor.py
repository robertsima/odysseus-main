"""Always-on monitor that auto-continues the agent when a background job
(see src/bg_jobs.py) finishes.

Reliability is the whole point: completion → agent re-invocation must never
silently no-op. The monitor drains `bg_jobs.pending_followups()` every tick and
only calls `mark_followed_up()` AFTER the agent run succeeds — so a transient
failure is simply retried on the next tick. A timed-out/dead job still produces
a follow-up ("the job failed/timed out"), so the user always hears back.
"""

from __future__ import annotations

import asyncio
import logging

from src import bg_jobs
from src.prompt_security import untrusted_context_message

logger = logging.getLogger(__name__)

_monitor_task = None
POLL_INTERVAL_S = 5
# The follow-up agent run is allowed a few rounds to actually continue the task
# (e.g. after `pip install` finishes, run the transcription).
_FOLLOWUP_MAX_ROUNDS = 12


# Jobs whose deferral was already logged. The monitor retries every
# POLL_INTERVAL_S while the chat is busy, and logging each retry wrote 824
# identical lines in one 4-hour bundle (2026-10-07).
_DEFERRAL_LOGGED: set = set()


def _background_result_message(rec):
    inject = (
        f"[Background job {rec['id']} finished]\n\n"
        f"{bg_jobs.result_text(rec)}\n\n"
        "Continue the task using this output. Don't repeat work that's already done. "
        "If the task is now complete, give the user the final result."
    )
    return untrusted_context_message("background job output", inject)


def _close_job_run(rec, title: str) -> None:
    from src import agent_activity as activity

    activity.run_finished(rec.get("session_id"), "bg_job", f"bg_job-{rec['id']}",
                          f"Background job {rec['id']}: {title}",
                          status="failed" if rec.get("status") == "failed" else "completed",
                          data={"job_id": rec["id"], "command": str(rec.get("command") or "")[:200],
                                "exit_code": rec.get("exit_code")})


def deliver_to_live_turn(session_id) -> list:
    """Result messages for the jobs this chat finished during its running turn.

    The agent loop calls this between rounds. Before, a job that ended mid-turn
    waited for the turn to end and then started a follow-up turn of its own:
    on 2026-10-07 a worker's two test runs finished while it worked, and after
    its hand-back each one re-ran the 100k-token chat and wrote the hand-back
    again. Claimed jobs are marked followed up, so the monitor skips them.
    """
    if not session_id:
        return []
    messages = []
    for rec in bg_jobs.take_finished(str(session_id)):
        _DEFERRAL_LOGGED.discard(rec["id"])
        messages.append(_background_result_message(rec))
        try:
            _close_job_run(rec, "read in the running turn")
        except Exception:
            logger.debug("bg job run close failed", exc_info=True)
        logger.info("bg-followup: job %s delivered into the running turn of session %s", rec["id"], session_id)
    return messages


async def _drain_agent(sess, messages, *, run_id=None):
    """Run the agent loop headless against a session. Returns
    (final_prose, tool_events) — tool_events in the same shape the live chat
    saves, so the frontend rebuilds them as standard agent-thread tool cards,
    including the `ask_user` card of a held exact-approval so the user can
    authorize it on the next foreground turn instead of losing it headlessly.

    The drain itself lives in :mod:`src.headless_agent` (shared with
    ``send_to_session`` in agent mode). It was open-coded here while
    ``run_headless`` still passed the upstream loop parameters it did not
    accept; it no longer does, and going back through it is what gets this run
    the owner's baseline denials, the chat's own stored tool policy, the
    resolved context window and its own row on the activity feed — none of
    which the open-coded loop call had.
    """
    from src.headless_agent import run_headless

    return await run_headless(
        sess, messages,
        max_rounds=_FOLLOWUP_MAX_ROUNDS,
        # A continuation of the user's own chat, not a sub-agent: the
        # anti-fan-out set does not apply, and the chat's own policy does —
        # the tools it has switched off and its approval mode, which
        # `run_headless` resolves from the chat itself. A finished worker's
        # continuation (`agent_control._continue_parent`) has the same shape
        # but additionally denies every launcher, so a worker's completion
        # can never start more workers.
        subagent=False,
        activity_session_id=getattr(sess, "id", None) if run_id else None,
        run_id=run_id,
        source="bg_job",
        owner=getattr(sess, "owner", None),
    )


async def _run_followup(rec: dict) -> bool:
    """Re-invoke the agent in the job's session with the result. Returns True
    if the follow-up completed (or there's nothing to do) — i.e. it's safe to
    mark followed_up. Returns False to retry on the next tick."""
    from src.ai_interaction import get_session_manager
    from core.models import ChatMessage

    sm = get_session_manager()
    if not sm:
        return False  # not ready yet — retry
    sess = sm.get_session(rec["session_id"])
    if not sess:
        # Session was deleted — nothing to continue. Consider it handled so we
        # don't retry forever.
        logger.info("bg-followup: session %s gone for job %s — skipping", rec.get("session_id"), rec.get("id"))
        return True

    # Don't write into a session that's mid-stream. The followup appends to
    # history + save_sessions(); a concurrent live turn does the same, and with
    # no per-session lock the two interleave (reordered/clobbered messages).
    # Defer — return False so we retry on the next tick once the turn finishes.
    # is_busy, not is_active: a worker's turn is headless (track_external), so
    # it has no stream. On 2026-10-06 the is_active check let a follow-up start
    # a second loop beside a running worker in the same worktree; each loop
    # took the other's edits for someone else's and the worker stopped blocked.
    try:
        from src import agent_runs
        if agent_runs.is_busy(sess.id):
            if rec.get("id") not in _DEFERRAL_LOGGED:
                _DEFERRAL_LOGGED.add(rec.get("id"))
                logger.info("bg-followup: session %s busy (live turn) — deferring job %s until the turn reads "
                            "it or ends", sess.id, rec.get("id"))
            return False
    except Exception:
        pass
    _DEFERRAL_LOGGED.discard(rec.get("id"))

    context = sess.get_context_messages()
    context.append(_background_result_message(rec))
    # Other jobs of this chat that finished too go into the same turn. On
    # 2026-10-07 three test runs that ended together each started a turn of
    # their own, and each turn wrote the worker's hand-back again.
    others = bg_jobs.take_finished(str(sess.id), exclude=(rec["id"],))
    for other in others:
        context.append(_background_result_message(other))

    from src import agent_activity as activity

    job_run = f"bg_job-{rec['id']}"
    activity.publish(sess.id, "status",
                     f"Background job {rec['id']} {'failed' if rec.get('status') == 'failed' else 'finished'}"
                     f" (exit {rec.get('exit_code')}) — continuing the chat",
                     source="bg_job", run_id=job_run, owner=getattr(sess, "owner", None),
                     data={"job_id": rec["id"], "exit_code": rec.get("exit_code"), "status": rec.get("status")},
                     detail=bg_jobs.result_text(rec)[:2000])
    from src import agent_runs

    # The chat is working again: every sidebar should show it.
    try:
        with agent_runs.track_external(sess.id, source="bg_job", owner=getattr(sess, "owner", None)):
            full, tool_events = await _drain_agent(sess, context, run_id=job_run)
    except BaseException:
        bg_jobs.release([other["id"] for other in others])
        raise
    for other in others:
        try:
            _close_job_run(other, f"read with job {rec['id']}")
        except Exception:
            logger.debug("bg job run close failed", exc_info=True)
    activity.run_finished(sess.id, "bg_job", job_run, f"Background job {rec['id']}: chat continued",
                          status="failed" if rec.get("status") == "failed" else "completed",
                          owner=getattr(sess, "owner", None),
                          data={"job_id": rec["id"], "command": str(rec.get("command") or "")[:200],
                                "exit_code": rec.get("exit_code"), "steps": len(tool_events),
                                "result_excerpt": full[:400]})

    # Persist ONLY the assistant continuation so it renders as a normal agent
    # turn — a standard chat bubble plus `tool_events` that the frontend
    # rebuilds into the usual agent-thread tool cards (chatRenderer:1494). The
    # trigger isn't saved as its own message (it'd be an out-of-place bubble);
    # the raw job output is stashed in metadata for traceability instead.
    sm.add_message(sess.id, ChatMessage(
        "assistant", full,
        metadata={
            "tool_events": tool_events,
            "model": sess.model,
            "bg_job_id": rec["id"],
            "bg_result": bg_jobs.result_text(rec)[:4000],
        },
    ))
    sm.save_sessions()
    logger.info("bg-followup: auto-continued session %s for job %s%s (%d chars, %d tools)",
                sess.id, rec["id"], f" and {len(others)} more" if others else "", len(full), len(tool_events))
    # In a worker chat this reply is the worker's latest result. Without the
    # hand-up it stayed in the worker chat and the parent (and the person) never
    # heard that the work had finished.
    if full.strip():
        from src.agent_control import _hand_up_when_done

        await _hand_up_when_done(sm, sess.id, sess, full, "completed", getattr(sess, "owner", None))
    return True


async def _loop():
    while True:
        try:
            for rec in bg_jobs.pending_followups():
                # A running turn may have claimed it since the list was read.
                if bg_jobs.is_followed_up(rec["id"]):
                    continue
                try:
                    if await _run_followup(rec):
                        bg_jobs.mark_followed_up(rec["id"])
                except Exception as e:
                    # Idempotent: leave followed_up=False so the next tick retries.
                    logger.warning("bg-followup failed for %s (will retry): %s", rec.get("id"), e)
        except Exception as e:
            logger.warning("bg-monitor tick error: %s", e)
        await asyncio.sleep(POLL_INTERVAL_S)


def start_bg_monitor():
    """Idempotent — start the always-on background-job monitor."""
    global _monitor_task
    if _monitor_task and not _monitor_task.done():
        return _monitor_task
    _monitor_task = asyncio.create_task(_loop())
    logger.info("Background-job monitor started (poll %ds)", POLL_INTERVAL_S)
    return _monitor_task
