"""How an agent may use bash and python, decided separately from vault access.

Until 2026-09-29 one per-chat setting, ``private_vault_access``, decided both
whether an agent may read private vault notes and how it gets a shell: with
the grant an unrestricted shell, without it a sandboxed one or, when the
workspace could not be sandboxed, none at all. So a planner that needed the
private notes got a full shell too, a coding agent that needed a shell had to
be handed the vault, and a worker bound to a broad folder lost bash entirely.

``shell_access`` is now its own setting, on a chat or a loadout:

* ``sandbox`` (the default) -- bash and python run in the bubblewrap sandbox
  (src/shell_sandbox.py), which holds only the workspace (plus its managed
  worktrees) and the read-only system; no app data, no vault. When the chat's
  workspace cannot be sandboxed (none, or a folder that holds the app's data),
  a private scratch folder is used instead, so the shell is never simply gone.
* ``host`` -- an unrestricted shell on the server, as the app's user. It can
  read anything that user can, the vault included, whatever the vault setting
  says; choosing it is choosing that.
* ``off`` -- no shell.

Vault access now means only what its name says: private notes reach the agent
through retrieval and the file tools. (File-reading MCP servers stay behind
the vault grant: they are file readers, not the shell.)

Chats and loadouts that had the vault grant before the split are moved to
``host`` once (``migrate_legacy``), so nothing that worked stops working.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

SETTING = "shell_access"
MODES = ("sandbox", "host", "off")
DEFAULT = "sandbox"
LABELS = {"sandbox": "Sandboxed", "host": "Full server shell", "off": "Off"}


def normalize(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    aliases = {"sandboxed": "sandbox", "full": "host", "unrestricted": "host", "none": "off", "disabled": "off"}
    text = aliases.get(text, text)
    return text if text in MODES else None


def resolve(settings: Optional[Dict[str, Any]]) -> str:
    """The shell mode a chat's stored settings give (``sandbox`` by default)."""
    return normalize((settings or {}).get(SETTING)) or DEFAULT


def resolve_for_session(session_id: Optional[str]) -> str:
    if not session_id:
        return DEFAULT
    try:
        from core.database import get_session_settings

        return resolve(get_session_settings(session_id) or {})
    except Exception:
        # Unreadable policy: the sandbox is the narrowest mode that still works.
        return DEFAULT


def scratch_workspace(session_id: Optional[str]) -> str:
    """A private folder for a chat whose own workspace cannot be sandboxed."""
    from src.constants import AGENT_WORKSPACE_DIR

    name = re.sub(r"[^A-Za-z0-9_-]+", "-", str(session_id or "shared")).strip("-")[:80] or "shared"
    path = os.path.join(AGENT_WORKSPACE_DIR, "sandbox", name)
    os.makedirs(path, exist_ok=True)
    return os.path.realpath(path)


def sandbox_workspace(workspace: Optional[str], session_id: Optional[str]) -> Tuple[Optional[str], str, str]:
    """Where a sandboxed shell runs: ``(path, why_not_workspace, why_unavailable)``.

    ``path`` is the workspace when the sandbox accepts it, else a scratch
    folder (``why_not_workspace`` says why); None only when the sandbox itself
    does not work on this server (``why_unavailable``).
    """
    from src import shell_sandbox

    why = shell_sandbox.unavailable_reason(workspace)
    if not why:
        return os.path.realpath(workspace), "", ""
    problem = shell_sandbox.workspace_problem(workspace)
    if not problem:
        # The workspace is fine; the sandbox itself does not work here.
        return None, "", why
    state = shell_sandbox.status()
    if not state.get("available"):
        return None, problem, str(state.get("reason") or "the sandbox is unavailable")
    try:
        return scratch_workspace(session_id), problem, ""
    except OSError as exc:
        return None, problem, f"no scratch folder could be made: {exc}"


_RANK = {"off": 0, "sandbox": 1, "host": 2}


def widened(current: Optional[Dict[str, Any]], patch: Dict[str, Any]) -> list:
    """What ``patch`` would widen on a chat: a wider shell, or the vault grant."""
    current = current or {}
    out = []
    if "shell_access" in patch and patch.get("shell_access") is not None:
        new = normalize(patch.get("shell_access")) or DEFAULT
        if _RANK[new] > _RANK[resolve(current)]:
            out.append(f"shell_access → {new}")
    if patch.get("private_vault_access") is True and current.get("private_vault_access") is not True:
        out.append("private_vault_access → on")
    return out


def guard_settings_change(request: Any, session_id: str, patch: Dict[str, Any]) -> None:
    """Refuse a chat-settings change a person did not make, or may not make.

    An agent reaches the app's API through its loopback tool (the internal tool
    header, possibly with its owner's name); it must not widen its own chat's
    shell or vault access, which is the one thing the Shell and vault settings
    exist to decide for it. A full server shell is an admin's choice.
    """
    from fastapi import HTTPException

    from core.middleware import INTERNAL_TOOL_HEADER
    from src.owner_identity import INTERNAL_TOOL_USER, auth_disabled

    try:
        from core.database import get_session_settings

        current = get_session_settings(session_id) or {}
    except Exception:
        current = {}
    wider = widened(current, patch)
    if not wider:
        return
    agent_call = (request.headers.get(INTERNAL_TOOL_HEADER) is not None
                  or getattr(request.state, "current_user", None) == INTERNAL_TOOL_USER
                  or getattr(request.state, "api_token", False))
    if agent_call:
        raise HTTPException(403, "Only a person in the Agamemnon UI can widen a chat's shell or vault access "
                                 f"({', '.join(wider)})")
    if normalize(patch.get("shell_access")) == "host" and not auth_disabled():
        mgr = getattr(request.app.state, "auth_manager", None)
        user = getattr(request.state, "current_user", None)
        if not (mgr and user and mgr.is_admin(user)):
            raise HTTPException(403, "Only an admin can give a chat a full server shell")


# ── one-time move of the old coupling ─────────────────────────────────────

_MARKER = "shell_access_split_v1"


def _marker_path() -> str:
    from src.constants import DATA_DIR

    return os.path.join(DATA_DIR, ".migrations", _MARKER)


def migrate_legacy() -> Dict[str, int]:
    """Give ``shell_access: host`` to every chat and loadout that had the vault
    grant before the split (what they effectively had), once.

    Runs once per data directory: afterwards a chat given vault access keeps
    whatever shell it has, which is the point of the split.
    """
    marker = _marker_path()
    if os.path.exists(marker):
        return {"chats": 0, "loadouts": 0, "skipped": 1}
    chats = loadouts = 0
    try:
        import json

        from core.database import Session, get_db_session

        with get_db_session() as db:
            for row in db.query(Session).filter(Session.settings_json.isnot(None)).all():
                try:
                    data = json.loads(row.settings_json or "{}")
                except (TypeError, ValueError):
                    continue
                if isinstance(data, dict) and data.get("private_vault_access") is True and SETTING not in data:
                    data[SETTING] = "host"
                    row.settings_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
                    chats += 1
    except Exception:
        logger.warning("shell access split: chat migration failed; will retry next start", exc_info=True)
        return {"chats": 0, "loadouts": 0, "failed": 1}
    try:
        from src.settings import load_settings, save_settings

        settings = load_settings()
        profiles = settings.get("agent_profiles")
        if isinstance(profiles, list):
            changed = False
            for profile in profiles:
                if isinstance(profile, dict) and profile.get("private_vault_access") is True and SETTING not in profile:
                    profile[SETTING] = "host"
                    loadouts += 1
                    changed = True
            if changed:
                save_settings(settings)
    except Exception:
        logger.warning("shell access split: loadout migration failed; will retry next start", exc_info=True)
        return {"chats": chats, "loadouts": 0, "failed": 1}
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(f"chats={chats} loadouts={loadouts}\n")
    except OSError:
        logger.warning("shell access split: could not write %s", marker, exc_info=True)
    if chats or loadouts:
        logger.info("[shell-access] split from vault access: %d chat(s) and %d loadout(s) that had the "
                    "vault grant keep a full server shell (shell_access=host)", chats, loadouts)
    return {"chats": chats, "loadouts": loadouts}
