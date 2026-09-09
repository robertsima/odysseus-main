"""Declarative personal-document directories from the environment.

A containerised deploy already declares its vault in the compose file (the bind
mounts), but the *index* of those trees lived only in admin-API state written
after first boot. A fresh install therefore came up with the vault mounted,
unindexed, and — worse — unlabelled, with no error anywhere: the operator had to
remember to POST /api/personal/add_directory again. ``ODYSSEUS_PERSONAL_DIRS``
moves that declaration next to the mounts, so the compose file alone is enough
to reproduce a working install.

Format (comma-separated, ``path:label`` pairs)::

    ODYSSEUS_PERSONAL_DIRS=Vault Mind:public,AI Mind:public,Journal:private

Paths are relative to PERSONAL_DIR (absolute paths are accepted but must resolve
inside it — same confinement the HTTP route applies). Labels are the
``rag_sensitivity`` vocabulary.

Two deliberate departures from ``normalize_sensitivity``:

  1. An unrecognised label is REJECTED, not coerced. ``normalize_sensitivity``
     maps anything that isn't exactly "private" to "public", which is the right
     default for untrusted input but the wrong one here: ``Journal:privat``
     would silently publish a journal to every hosted model. A typo has to fail
     loudly, and it fails *closed* (the entry is skipped, so the directory stays
     unindexed rather than being indexed public).
  2. The label is mandatory. A bare ``Journal`` would inherit "public" by
     omission, which is the same trap wearing a different hat.

Reconciliation is additive: directories the operator added through the API are
never removed just because they are absent from the variable. The variable
declares what must exist, not the complete set. Declared directories are
application-scoped and do not require an authenticated owner at startup.
"""
import json
import logging
import os
from typing import List, NamedTuple, Optional, Tuple

from src.rag_sensitivity import VALID_SENSITIVITIES

logger = logging.getLogger(__name__)

ENV_VAR = "ODYSSEUS_PERSONAL_DIRS"


class DeclaredDirectory(NamedTuple):
    """One ``path:label`` entry, before path resolution."""
    path: str
    sensitivity: str


def parse_declarations(raw: Optional[str]) -> Tuple[List[DeclaredDirectory], List[str]]:
    """Parse the raw variable into entries plus human-readable errors.

    Returns ``(entries, errors)`` rather than raising: one malformed entry must
    not discard the well-formed ones, and the caller logs every error so a typo
    is visible in the startup log instead of silently doing nothing.
    """
    entries: List[DeclaredDirectory] = []
    errors: List[str] = []

    for chunk in (raw or "").split(","):
        item = chunk.strip()
        if not item:
            continue
        # rsplit: a Windows path ("C:\\vault") contains a colon of its own, so
        # only the LAST colon separates the label.
        path, sep, label = item.rpartition(":")
        if not sep:
            errors.append(
                f"{item!r} has no sensitivity label — expected 'path:public' or 'path:private'"
            )
            continue
        path = path.strip()
        label = label.strip().lower()
        if not path:
            errors.append(f"{item!r} has an empty path")
            continue
        if label not in VALID_SENSITIVITIES:
            errors.append(
                f"{item!r} has unknown sensitivity {label!r} — "
                f"expected one of {', '.join(VALID_SENSITIVITIES)}"
            )
            continue
        entries.append(DeclaredDirectory(path=path, sensitivity=label))

    return entries, errors


def resolve_declared_path(path: str, personal_dir: str) -> Optional[str]:
    """Resolve one declared path against PERSONAL_DIR, or None if it escapes.

    Mirrors ``_resolve_allowed_personal_dir`` in routes/personal_routes.py:
    realpath (not abspath) so a symlink pointing outside the root is resolved
    before the containment check, which abspath would let through.
    """
    base_abs = os.path.realpath(personal_dir)
    candidate = path if os.path.isabs(path) else os.path.join(base_abs, path)
    resolved = os.path.realpath(candidate)
    try:
        if os.path.commonpath([resolved, base_abs]) != base_abs:
            return None
    except ValueError:  # different drives on Windows
        return None
    return resolved


def resolve_owner(explicit: Optional[str] = None) -> Optional[str]:
    """Owner to record on chunks indexed from declared directories.

    Chunks must carry an owner or they fall outside the owner-filtered search
    and become unretrievable (see ``rag_vector.owner_for_directory``), and there
    is no request user at startup. Resolved from auth.json the same way
    companion/pairing.find_admin_user does: the flagged admin, else the first
    user.
    """
    if explicit:
        return explicit
    from src.constants import AUTH_FILE

    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    users = data.get("users") or {}
    if not isinstance(users, dict):
        return None
    for username, udata in users.items():
        if isinstance(udata, dict) and udata.get("is_admin") is True:
            return username
    return next(iter(users), None)


def reconcile(manager, raw: Optional[str] = None, *, owner: Optional[str] = None) -> dict:
    """Apply ``ODYSSEUS_PERSONAL_DIRS`` to a PersonalDocsManager.

    Additive and idempotent:
      * untracked directory  -> tracked, labelled, and indexed
      * tracked, label drift -> relabelled (which also restamps its chunks)
      * tracked, same label  -> untouched

    Never raises: a bad variable must not stop the app from booting, so every
    failure is logged and reported in the returned summary instead.
    """
    if raw is None:
        raw = os.environ.get(ENV_VAR)
    summary = {"added": [], "relabelled": [], "unchanged": [], "errors": []}
    if not (raw or "").strip():
        return summary

    entries, errors = parse_declarations(raw)
    for message in errors:
        logger.error("%s: %s", ENV_VAR, message)
    summary["errors"].extend(errors)

    if not entries:
        return summary

    personal_dir = getattr(manager, "personal_dir", None)
    if not personal_dir:
        summary["errors"].append("manager has no personal_dir")
        return summary

    tracked = {
        os.path.abspath(d) for d in (getattr(manager, "indexed_directories", None) or [])
    }

    for entry in entries:
        resolved = resolve_declared_path(entry.path, personal_dir)
        if not resolved:
            message = f"{entry.path!r} resolves outside {personal_dir} — skipped"
            logger.error("%s: %s", ENV_VAR, message)
            summary["errors"].append(message)
            continue
        if not os.path.isdir(resolved):
            message = f"{resolved} is not a directory — skipped (is the mount present?)"
            logger.error("%s: %s", ENV_VAR, message)
            summary["errors"].append(message)
            continue

        current = (getattr(manager, "directory_sensitivity", None) or {}).get(resolved)
        try:
            if resolved not in tracked:
                manager.add_directory(
                    resolved, index=True, owner=owner, sensitivity=entry.sensitivity
                )
                summary["added"].append({"directory": resolved, "sensitivity": entry.sensitivity})
                logger.info(
                    "%s: indexed %s as %s", ENV_VAR, resolved, entry.sensitivity
                )
            elif current != entry.sensitivity:
                manager.set_directory_sensitivity(resolved, entry.sensitivity)
                summary["relabelled"].append(
                    {"directory": resolved, "from": current, "to": entry.sensitivity}
                )
                logger.info(
                    "%s: relabelled %s %s -> %s", ENV_VAR, resolved, current, entry.sensitivity
                )
            else:
                summary["unchanged"].append(
                    {"directory": resolved, "sensitivity": entry.sensitivity}
                )
        except Exception as e:
            message = f"{resolved}: {e}"
            logger.error("%s: failed to apply — %s", ENV_VAR, message)
            summary["errors"].append(message)

    return summary
