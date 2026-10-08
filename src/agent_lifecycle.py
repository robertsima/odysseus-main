"""Owner-scoped session lifecycle, shared by HTTP and tool entrypoints.

The lock coordinates worker registration in the single application process.
Persisted flags and restore markers commit together before memory is updated.
"""
import json
import logging
from threading import RLock

from fastapi import HTTPException

unit_lock = RLock()


def _settings(row):
    data = json.loads(row.settings_json or "{}")
    if not isinstance(data, dict):
        raise ValueError("Invalid session settings")
    return data


def change_archive(session_id, owner, manager, *, restore=False):
    from core import database
    from src import agent_activity, agent_control, agent_runs, tool_approvals

    with unit_lock:
        try:
            with database.get_db_session() as db:
                rows = {r.id: r for r in db.query(database.Session).filter(
                    database.Session.owner == owner).all()}
                if session_id not in rows:
                    raise HTTPException(404, "Session not found")
                try:
                    settings = {sid: database.get_session_settings(sid, strict=True) for sid in rows}
                except Exception as exc:
                    raise HTTPException(409, "Could not verify child work; try again") from exc
                if restore:
                    chain = {session_id}
                    parent = settings[session_id].get("parent_session")
                    while parent in rows and rows[parent].archived and parent not in chain:
                        chain.add(parent)
                        parent = settings[parent].get("parent_session")
                    targets = chain | {sid for sid, row in rows.items()
                                       if row.archived and settings[sid].get("archived_with") in chain}
                    for sid in targets:
                        rows[sid].archived = False
                        settings[sid].pop("archived_with", None)
                        rows[sid].settings_json = json.dumps(settings[sid], sort_keys=True)
                    workers = []
                else:
                    targets = {session_id}
                    while True:
                        expanded = targets | {sid for sid in rows
                                              if settings[sid].get("parent_session") in targets}
                        if expanded == targets:
                            break
                        targets = expanded
                    active = sorted(sid for sid in targets if agent_runs.is_busy(sid)
                                    or agent_activity.has_active_run(sid)
                                    or agent_control.live_children(sid) > 0
                                    or tool_approvals.tool_approval_store.has_pending_for_session(sid))
                    if active:
                        raise HTTPException(409, "Stop active work before archiving: " + ", ".join(active))
                    workers = database.archive_session_unit(session_id, targets, owner, db)
                    targets = {session_id, *workers}
                db.flush()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, "Could not restore this chat" if restore
                                else "Could not archive this chat") from exc
        for sid in targets:
            session = getattr(manager, "sessions", {}).get(sid)
            if session is not None:
                session.archived = not restore
            elif restore and hasattr(manager, "_load_session_from_db"):
                try:
                    manager._load_session_from_db(sid)
                except Exception:
                    logging.getLogger(__name__).warning("Restored session %s will reload on access", sid)
        return {"ok": True, "session_id": session_id, "archived": not restore,
                "restored": sorted(targets) if restore else [],
                "archived_workers": sorted(workers),
                "message": "Restored." if restore else "Archived. Chats and run history are preserved."}
