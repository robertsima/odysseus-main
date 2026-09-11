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

logger = logging.getLogger(__name__)

_monitor_task = None
POLL_INTERVAL_S = 5
# The follow-up agent run is allowed a few rounds to actually continue the task
# (e.g. after `pip install` finishes, run the transcription).
_FOLLOWUP_MAX_ROUNDS = 12


async def _drain_agent(sess, messages, *, run_id=None):
    """Run the agent loop headless against a session. Returns
    (final_prose, tool_events) — tool_events in the same shape the live chat
    saves, so the frontend rebuilds them as standard agent-thread tool cards.
    The drain itself lives in :mod:`src.headless_agent` (shared with
    ``send_to_session`` in agent mode); the follow-up keeps every tool the
    chat had, since it is the same chat continuing."""
    from src.headless_agent import run_headless

    return await run_headless(
        sess, messages,
        max_rounds=_FOLLOWUP_MAX_ROUNDS,
        # A continuation of the user's own chat, not a sub-agent: nothing is
        # blocked beyond what the chat already had.
        disabled_tools=None,
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
    try:
        from src import agent_runs
        if agent_runs.is_active(sess.id):
            logger.info("bg-followup: session %s busy (live turn) — deferring job %s", sess.id, rec.get("id"))
            return False
    except Exception:
        pass

    inject = (
        f"[Background job {rec['id']} finished]\n\n"
        f"{bg_jobs.result_text(rec)}\n\n"
        "Continue the task using this output. Don't repeat work that's already done. "
        "If the task is now complete, give the user the final result."
    )
    context = sess.get_context_messages()
    context.append({"role": "user", "content": inject})

    from src import agent_activity as activity

    job_run = f"bg_job-{rec['id']}"
    activity.publish(sess.id, "status",
                     f"Background job {rec['id']} {'failed' if rec.get('status') == 'failed' else 'finished'}"
                     f" (exit {rec.get('exit_code')}) — continuing the chat",
                     source="bg_job", run_id=job_run, owner=getattr(sess, "owner", None),
                     data={"job_id": rec["id"], "exit_code": rec.get("exit_code"), "status": rec.get("status")},
                     detail=bg_jobs.result_text(rec)[:2000])
    full, tool_events = await _drain_agent(sess, context, run_id=job_run)
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
    logger.info("bg-followup: auto-continued session %s for job %s (%d chars, %d tools)",
                sess.id, rec["id"], len(full), len(tool_events))
    return True


async def _loop():
    while True:
        try:
            for rec in bg_jobs.pending_followups():
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
