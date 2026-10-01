"""Read access to the files attached to a chat and to the workers it started.

Incident, 2026-10-01: the user attached a Penpot export to the admin chat. The
admin chat could read the stored upload, but every worker it delegated to was
refused (`path ... is outside the workspace and outside the personal documents
directory`): a worker is bound to its own workspace, and its bash/python run in
a sandbox that shows only that workspace. One worker asked the user to
re-supply a file that was already on the server; another reported the artwork
as "could not inspect". The orchestrator ended up unzipping it itself.

How uploads are linked (there is no per-session upload table):

* ``uploads.json`` (``UploadHandler``) maps an upload id to its file path and
  its ``owner``.
* A chat message that carries attachments stores their ids in its metadata,
  ``{"attachments": [{"attachment_id": ...}]}`` (``attachment_refs``). When a
  chat is compacted those messages move to ``chat_message_archive`` with the
  same metadata, so both tables are read.
* A worker chat records its parent in its session settings
  (``parent_session``), written by ``agent_control`` when it starts the worker.

So a chat's attachments are the upload ids referenced by its own user messages,
and a worker may read those of every ancestor. Nothing here widens the file
tools beyond that exact set of files: the caller (``tool_execution``) still
applies the sensitive/app-state/hard-link refusals, and grants READ access only.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import time
from typing import Dict, FrozenSet, Optional, Tuple

logger = logging.getLogger(__name__)

# A worker can start workers; the cap stops a corrupted parent cycle or an
# absurd chain from turning one path check into an unbounded walk.
MAX_LINEAGE_DEPTH = 8
# Bounds both the scan of one chat's messages and what a sandbox may be handed.
MAX_LINEAGE_ATTACHMENTS = 200
MAX_SANDBOX_ATTACHMENTS = 20

_CACHE_TTL_S = 5.0
_CACHE: Dict[Tuple[str, Optional[str]], Tuple[float, FrozenSet[str]]] = {}

# The session whose tool call is running, set once per call by
# execute_tool_block next to the active workspace. contextvars are task-local,
# and asyncio.to_thread copies them, so file tools running in a thread see it.
_current: contextvars.ContextVar = contextvars.ContextVar("attachment_access_session", default=None)


def bind(session_id: Optional[str], owner: Optional[str]):
    """Make ``session_id``/``owner`` the identity attachment lookups use."""
    return _current.set((str(session_id), owner) if session_id else None)


def unbind(token) -> None:
    _current.reset(token)


def current_identity() -> Optional[Tuple[str, Optional[str]]]:
    return _current.get()


def clear_cache() -> None:
    _CACHE.clear()


def _same_owner(row_owner: Optional[str], owner: Optional[str]) -> bool:
    return (row_owner or None) == (owner or None)


def _lineage_session_ids(db, session_id: str, owner: Optional[str]) -> list[str]:
    """``session_id`` and its ancestors, nearest first, all owned by ``owner``.

    The walk stops at the first session that is missing, owned by someone
    else, or already seen, so a worker never reaches another user's chat
    through a forged ``parent_session`` setting.
    """
    from core.database import Session as DbSession

    chain: list[str] = []
    current: Optional[str] = session_id
    while current and current not in chain and len(chain) < MAX_LINEAGE_DEPTH:
        row = db.query(DbSession.owner, DbSession.settings_json).filter(DbSession.id == current).first()
        if row is None or not _same_owner(row[0], owner):
            break
        chain.append(current)
        try:
            settings = json.loads(row[1]) if row[1] else {}
        except (TypeError, ValueError):
            settings = {}
        parent = settings.get("parent_session") if isinstance(settings, dict) else None
        current = str(parent) if parent else None
    return chain


def _attachment_ids(db, session_ids: list[str]) -> list[str]:
    from core.database import ArchivedChatMessage, ChatMessage
    from src.attachment_refs import attachment_refs_from_metadata

    found: list[str] = []
    for model in (ChatMessage, ArchivedChatMessage):
        rows = (
            db.query(model.meta_data)
            .filter(model.session_id.in_(session_ids), model.role == "user", model.meta_data.like("%attachment%"))
            .all()
        )
        for (raw,) in rows:
            try:
                meta = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                continue
            if not isinstance(meta, dict):
                continue
            for ref in attachment_refs_from_metadata(meta):
                att_id = ref.get("attachment_id")
                if att_id and att_id not in found:
                    found.append(att_id)
                    if len(found) >= MAX_LINEAGE_ATTACHMENTS:
                        return found
    return found


def _upload_path(upload_handler, att_id: str, owner: Optional[str]) -> Optional[str]:
    """Real path of an owner's upload, or None. Read-only: unlike
    ``reserve_upload`` this does not touch the index."""
    try:
        info = upload_handler.get_upload_info(att_id)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(info, dict) or not _same_owner(info.get("owner"), owner):
        return None
    path = info.get("path")
    if not path:
        return None
    try:
        real = os.path.realpath(path)
        # The index row is data on disk, not authority: the file must sit inside
        # the upload root and carry the id as its name, as reserve_upload demands.
        if not os.path.isfile(real) or os.path.basename(real) != att_id:
            return None
        if not upload_handler._inside_upload_dir(real):
            return None
    except Exception:  # noqa: BLE001
        return None
    return real


def lineage_attachment_paths(session_id: Optional[str], owner: Optional[str]) -> FrozenSet[str]:
    """Real paths of the attachments of ``session_id`` and its ancestors.

    Same owner only. Empty on any failure: this only ever *adds* access, so an
    error must leave the caller with the refusal it had before.
    """
    if not session_id:
        return frozenset()
    key = (str(session_id), owner or None)
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < _CACHE_TTL_S:
        return cached[1]
    paths: FrozenSet[str] = frozenset()
    try:
        from core import database
        from src.tool_utils import get_upload_handler

        handler = get_upload_handler()
        if handler is not None:
            db = database.SessionLocal()
            try:
                chain = _lineage_session_ids(db, str(session_id), owner)
                ids = _attachment_ids(db, chain) if chain else []
            finally:
                db.close()
            found = (_upload_path(handler, att_id, owner) for att_id in ids)
            paths = frozenset(p for p in found if p)
    except Exception:  # noqa: BLE001
        logger.warning("attachment lineage lookup failed for session %s", session_id, exc_info=True)
    _CACHE[key] = (now, paths)
    return paths


def current_attachment_paths() -> FrozenSet[str]:
    """The lineage attachments of the session whose tool call is running."""
    ident = _current.get()
    if not ident:
        return frozenset()
    return lineage_attachment_paths(*ident)


def is_lineage_attachment(resolved: str) -> bool:
    """True when ``resolved`` (a realpath) is one of the current lineage's files."""
    paths = current_attachment_paths()
    if not paths:
        return False
    norm = os.path.normcase(resolved)
    return any(os.path.normcase(p) == norm for p in paths)


def sandbox_read_only_binds() -> Dict[str, str]:
    """``{host: inside}`` read-only binds for the current lineage's attachments,
    for ``shell_sandbox.build_argv(extra_ro_binds=...)``. Files only, capped,
    identity-mapped so the path the model was given works inside the sandbox."""
    binds: Dict[str, str] = {}
    for path in sorted(current_attachment_paths()):
        if len(binds) >= MAX_SANDBOX_ATTACHMENTS:
            break
        if os.path.isfile(path):
            binds[path] = path
    return binds
