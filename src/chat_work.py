"""What the user's chats are doing right now, for agents to read and act on.

The Agents room and the Workbench show running chats and can stop them; an
agent could not. On 2026-09-30 the admin agent, asked about a chat stuck 43
minutes in a silent bash call, answered that the chat was not one of its
workers and that it could not inspect or stop it — and, told about "the
implement feature engineer", started a second Lead Engineer instead of
looking at the one already working. ``running_chats`` lists every chat turn
and delegated run working now, with the tool each is running;
``manage_session`` exposes it (action ``running``) together with ``stop``,
and ``manage_agent_loadout start`` checks it before starting a loadout that
is already busy elsewhere.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _settings(session_id: str) -> Dict[str, Any]:
    try:
        from core.database import get_session_settings

        return get_session_settings(session_id) or {}
    except Exception:
        return {}


def running_chats(owner: Optional[str], sessions: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Chat turns and delegated runs of ``owner``'s chats that are working now."""
    from src import agent_activity as activity
    from src import agent_runs

    if sessions is None:
        try:
            from core.models import get_session_manager_instance

            manager = get_session_manager_instance()
            sessions = manager.get_sessions_for_user(owner) if manager else {}
        except Exception:
            sessions = {}
    owned = set(sessions or {})
    now = time.time()
    rows: List[Dict[str, Any]] = []
    for rec in agent_runs.list_runs(owned):
        if rec.get("status") != "running":
            continue
        sid = rec["session_id"]
        row = agent_runs.describe(sid) or {"session_id": sid, "started_at": rec.get("started_at"),
                                           "elapsed_s": round(now - (rec.get("started_at") or now))}
        settings = _settings(sid)
        row.update({"kind": "chat turn" if rec.get("source") == "chat" else f"{rec.get('source')} turn",
                    "name": getattr(sessions.get(sid), "name", "") or sid,
                    "profile": settings.get("agent_profile") or "",
                    "parent_session": settings.get("parent_session") or ""})
        rows.append(row)
    try:
        runs = activity.list_runs(owner=owner, limit=200, active_only=True)
    except Exception:
        runs = []
    for rec in runs:
        if rec.get("source") == "odysseus" or rec.get("status") != "running":
            continue
        summary = rec.get("summary") or {}
        sid = str(summary.get("target_session") or rec.get("session_id") or "")
        if owned and sid not in owned and rec.get("session_id") not in owned:
            continue
        if any(r["session_id"] == sid and r.get("kind") == "chat turn" for r in rows):
            continue
        rows.append({
            "session_id": sid, "kind": rec.get("source") or "run", "run_id": rec.get("run_id"),
            "name": getattr((sessions or {}).get(sid), "name", "") or rec.get("title") or sid,
            "title": rec.get("title") or "", "started_at": rec.get("started_at"),
            "elapsed_s": round(now - (rec.get("started_at") or now)),
            "profile": summary.get("profile") or _settings(sid).get("agent_profile") or "",
            "parent_session": summary.get("parent_session") or "",
        })
    rows.sort(key=lambda r: -(r.get("elapsed_s") or 0))
    return rows


def _duration(seconds: Any) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "?"
    return f"{s}s" if s < 60 else f"{s // 60}m" if s < 3600 else f"{s // 3600}h {s % 3600 // 60}m"


def describe_rows(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "None of your chats is working right now."
    lines = [f"{len(rows)} working now:"]
    for r in rows:
        who = f"{r.get('name') or r['session_id']} ({r['session_id']})"
        bits = [f"{r.get('kind')} for {_duration(r.get('elapsed_s'))}"]
        if r.get("profile"):
            bits.append(f"loadout {r['profile']}")
        if r.get("round"):
            bits.append(f"round {r['round']}, {r.get('tools_finished', 0)} tool calls done")
        line = f"- {who}: " + ", ".join(bits)
        if r.get("tool"):
            line += f"; running `{r['tool']}` for {_duration(r.get('tool_elapsed_s'))}"
            if r.get("command"):
                line += f": `{' '.join(str(r['command']).split())[:200]}`"
            tail = str(r.get("output_tail") or "").strip()
            line += f"\n  last output: {tail[-400:]}" if tail else "\n  (no output from it yet)"
        elif r.get("title"):
            line += f"; {r['title']}"
        lines.append(line)
    lines.append("Stop one with manage_session {\"action\": \"stop\", \"session_id\": \"<id>\"}; tell a running "
                 "agent something with message_agent.")
    return "\n".join(lines)


async def stop_chat(session_id: str, *, by: str) -> Dict[str, Any]:
    """Stop a chat's running turn and the workers and jobs working for it."""
    from src import agent_control, agent_runs

    turn = agent_runs.stop(session_id)
    workers = await agent_control.stop_chat_work(session_id, by=by)
    logger.info("[chat-work] stopped session=%s turn=%s workers=%s by=%s", session_id, turn, workers, by)
    return {"turn_stopped": bool(turn), "workers_stopped": workers}
