"""Shared public/private sensitivity policy for indexed documents.

Single source of the label vocabulary and its normalization so every layer that
touches it — the vector store (``rag_vector``), the directory tracker
(``personal_docs``), the HTTP routes and the agent tool surface — applies the
same rule and cannot drift, the same way ``index_walk`` single-sources the
directory-walk policy.

The label answers exactly one question: *may this chunk leave the machine?*
``private`` chunks are only ever retrieved for a session whose endpoint is
local (see ``model_context.is_local_endpoint``); ``public`` chunks go to any
model, including hosted APIs.

Unlabeled content is treated as ``public``. Chunks indexed before this feature
existed were already being sent to whatever model the session used, so
defaulting them to ``public`` preserves behaviour instead of silently emptying
an existing index. ``private`` is never inferred — a user has to ask for it.
"""
import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

SENSITIVITY_PUBLIC = "public"
SENSITIVITY_PRIVATE = "private"

VALID_SENSITIVITIES = (SENSITIVITY_PUBLIC, SENSITIVITY_PRIVATE)

# Metadata key used in Chroma chunk metadata and in PersonalDocsManager entries.
SENSITIVITY_KEY = "sensitivity"


def normalize_sensitivity(value: Any) -> str:
    """Coerce arbitrary input to a valid label, defaulting to ``public``.

    Anything that is not the exact string ``private`` (case/space-insensitive)
    is ``public``. Callers therefore cannot accidentally create a third label
    that would be excluded by every filter and become unretrievable.
    """
    if not isinstance(value, str):
        return SENSITIVITY_PUBLIC
    normalized = value.strip().lower()
    return SENSITIVITY_PRIVATE if normalized == SENSITIVITY_PRIVATE else SENSITIVITY_PUBLIC


def is_private(value: Any) -> bool:
    """True only for an explicit ``private`` label."""
    return normalize_sensitivity(value) == SENSITIVITY_PRIVATE


def metadata_is_private(metadata: Optional[Dict[str, Any]]) -> bool:
    """True when a chunk's metadata carries an explicit ``private`` label.

    A missing key is ``public`` — see the module docstring on why unlabeled
    content stays visible.
    """
    if not isinstance(metadata, dict):
        return False
    return is_private(metadata.get(SENSITIVITY_KEY))


# Name of the file PersonalDocsManager persists its directory labels to, inside
# PERSONAL_DIR. Kept here (not imported from personal_docs) so the file-tool
# layer can consult the labels without importing the document stack.
SENSITIVITY_STATE_FILENAME = "directory_sensitivity.json"

# Cache keyed on the state file's mtime: this is consulted on every file-tool
# path check, and re-reading the JSON per call would put disk I/O in that path.
_private_dirs_cache: Dict[str, Any] = {"mtime": None, "dirs": ()}


def private_directories() -> Tuple[str, ...]:
    """Absolute directories currently labelled private.

    Read from PERSONAL_DIR's state file rather than from a live
    PersonalDocsManager so callers in the tool layer stay decoupled from the
    document stack. A missing file means nothing is labelled private, which is
    correct — the label is only ever set explicitly. A *corrupt* file keeps the
    last known set instead of clearing it, so a bad write cannot silently
    unprotect content.
    """
    from src.constants import PERSONAL_DIR

    path = os.path.join(PERSONAL_DIR, SENSITIVITY_STATE_FILENAME)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _private_dirs_cache["mtime"] = None
        _private_dirs_cache["dirs"] = ()
        return ()

    if _private_dirs_cache["mtime"] != mtime:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
            if not isinstance(stored, dict):
                raise ValueError("directory sensitivity must be an object")
            _private_dirs_cache["dirs"] = tuple(
                os.path.abspath(key)
                for key, value in stored.items()
                if isinstance(key, str) and is_private(value)
            )
            _private_dirs_cache["mtime"] = mtime
        except Exception as e:
            # Keep the previous set; do not fall open on a partial/corrupt read.
            logger.warning("Could not read %s (%s); keeping previous private set", path, e)

    return _private_dirs_cache["dirs"]


def path_is_under_private_directory(path: str) -> bool:
    """True when ``path`` is inside (or is) a directory labelled private.

    Path-boundary match, not a prefix match, so a private ``/docs`` does not
    also capture ``/docs2``.
    """
    if not isinstance(path, str) or not path:
        return False
    abs_path = os.path.abspath(path)
    for directory in private_directories():
        if abs_path == directory or abs_path.startswith(directory + os.sep):
            return True
    return False


def apply_sensitivity(metadata: Dict[str, Any], sensitivity: Any = None) -> Dict[str, Any]:
    """Return a copy of ``metadata`` carrying a normalized sensitivity label.

    An explicit ``sensitivity`` argument wins; otherwise a label already in the
    metadata is kept; otherwise the chunk is labelled ``public``. Every chunk
    written through this helper carries the key, which is what lets the search
    filter use a plain equality match instead of relying on Chroma's
    (version-dependent) semantics for documents missing a metadata field.
    """
    result = dict(metadata or {})
    if sensitivity is not None:
        result[SENSITIVITY_KEY] = normalize_sensitivity(sensitivity)
    else:
        result[SENSITIVITY_KEY] = normalize_sensitivity(result.get(SENSITIVITY_KEY))
    return result
