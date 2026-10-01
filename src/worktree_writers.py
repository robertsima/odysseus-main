"""One writer per worktree: refuse file writes into a folder a live worker owns.

2026-10-01: an orchestrator chat started Lead Engineer workers whose workspace
was the worktree ``agamemnon-layout-identity-v3`` and, while they ran, kept
``apply_patch``-ing and ``edit_file``-ing that same worktree itself. Workers
then found files they had been told existed missing, and were told "the parent
has made uncommitted changes" and had to reconcile. The harness prompt already
says "keep one writer per repository or worktree at a time"; nothing enforced
it.

``agent_control.launch_worker`` registers each live worker's workspace here and
removes it when the run ends. The write tools (write_file, edit_file,
apply_patch) call :func:`conflict` first. Reads never come here.

Who may write into a worker's workspace:

* the worker itself, and any session below it (its own sub-workers);
* another live worker whose own workspace also contains the path. Workers that
  were handed the same checkout share it by design (the orchestrator chose
  that); refusing them would deadlock parallel workers on a shared workspace,
  and the evidence above is about a *chat* editing under its workers.

Everything else (the chat that launched the worker, an unrelated chat of the
same owner) is refused with a message that says what to do instead.

The registry is in memory and process-local, like ``agent_control._WORKERS``:
a worker run is a task of this process, so a restart ends it too. A lookup
failure fails open (logged, write allowed): a broken guard must not block every
write.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Dict, Iterable, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# run_id -> {"session_id", "parent", "owner", "workspace", "label"}
_LIVE: Dict[str, dict] = {}


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(str(path))))


def _inside(path: str, root: str) -> bool:
    if path == root:
        return True
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # different drives on Windows
        return False


def register(run_id: str, *, session_id: str, parent_session: Optional[str], owner: Optional[str],
             workspace: Optional[str], label: str) -> None:
    """Record that ``session_id`` is running in ``workspace``. A worker with no
    workspace has nothing to protect and is not recorded."""
    if not run_id or not workspace:
        return
    try:
        entry = {"session_id": str(session_id), "parent": str(parent_session) if parent_session else None,
                 "owner": owner, "workspace": _norm(workspace), "label": label}
    except Exception:
        logger.debug("worktree writer registration failed", exc_info=True)
        return
    with _lock:
        _LIVE[str(run_id)] = entry


def unregister(run_id: Optional[str]) -> None:
    with _lock:
        _LIVE.pop(str(run_id), None)


def _ancestors(session_id: str, live: Dict[str, dict]) -> set:
    """``session_id`` and the live workers above it (parent links recorded at launch)."""
    parents = {e["session_id"]: e["parent"] for e in live.values()}
    chain, sid = set(), session_id
    while sid and sid not in chain and len(chain) < 12:
        chain.add(sid)
        sid = parents.get(sid)
    return chain


def conflict(session_id: Optional[str], owner: Optional[str], paths: Iterable[str]) -> Optional[str]:
    """The refusal text when ``session_id`` may not write ``paths`` now, else None.

    Fails open: any error is logged and the write is allowed.
    """
    if not _LIVE or not session_id:
        return None
    try:
        sid = str(session_id)
        with _lock:
            live = dict(_LIVE)
        targets = [_norm(p) for p in paths]
        chain = _ancestors(sid, live)
        mine = [e["workspace"] for e in live.values() if e["session_id"] == sid]
        for entry in live.values():
            if owner is not None and entry["owner"] is not None and entry["owner"] != owner:
                continue
            if entry["session_id"] in chain:
                continue  # the worker itself, or a sub-worker below it
            hit = next((t for t in targets if _inside(t, entry["workspace"])), None)
            if hit is None:
                continue
            if any(_inside(hit, ws) for ws in mine):
                continue  # co-tenant worker on a workspace they were both given
            return (f"{entry['label']} (worker {entry['session_id']}) is working in {entry['workspace']}, "
                    f"which contains {hit}. Only one writer per worktree at a time: wait for its "
                    "hand-back (manage_agent_loadout status with wait_seconds), send it the change "
                    "with send_to_session, or stop it first. Reads are not affected.")
    except Exception:
        logger.warning("worktree writer check failed; allowing the write", exc_info=True)
    return None
