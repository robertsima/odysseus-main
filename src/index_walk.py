"""Shared directory-walk pruning for personal-document indexing (#5559).

Single source of the hidden-dir / junk-dir / hidden-file skip so the vector
index (``rag_vector.index_personal_documents``) and the keyword index
(``personal_docs.load_personal_index``) apply the exact same policy and cannot
drift — the drift is what left the keyword path sweeping in `.obsidian/`,
`.git/`, and `node_modules/` after the vector path was fixed.
"""
from typing import List, Set

# Well-known non-hidden junk directories to skip. Matched case-insensitively so
# a `Node_Modules` on a case-insensitive filesystem (macOS default) is still
# pruned. Hidden directories (dot-prefixed) are pruned separately. Kept
# deliberately small: over-pruning would silently drop a user's real content
# (e.g. a notes directory legitimately named "build").
EXCLUDED_DIR_NAMES: Set[str] = {'node_modules', '__pycache__', 'venv'}


def prune_index_dirs(dirs: List[str]) -> None:
    """In-place ``os.walk`` (topdown) directory prune: drop hidden and known
    junk directories so the walk never descends into them.

    The explicitly-targeted walk root is never a member of ``dirs`` (it is the
    ``dirpath`` argument), so it stays exempt — a user who deliberately points
    indexing at a hidden directory gets its contents, minus nested junk.
    """
    dirs[:] = [
        d for d in dirs
        if not d.startswith('.') and d.lower() not in EXCLUDED_DIR_NAMES
    ]


# Files PersonalDocsManager writes into PERSONAL_DIR to persist its own state.
# These live alongside the user's documents and match DEFAULT_EXTENSIONS, so
# without an explicit skip the indexers sweep them in as ordinary content.
# directory_sensitivity.json is the dangerous one: it maps private directory
# paths to their labels, so indexing it publishes exactly what the label is
# meant to protect — the existence, paths, and privacy of those trees — to any
# model allowed to read public documents.
STATE_FILENAMES: Set[str] = {
    'indexed_directories.json',
    'excluded_files.json',
    'directory_sensitivity.json',
}


def is_indexable_file(name: str) -> bool:
    """A file is indexable only if it is neither hidden nor tracker state.

    The state-file check is on the bare name rather than the full path: the
    indexers only ever have the basename here, and these names are specific
    enough that skipping them anywhere in a tree is a better trade than
    letting a nested copy leak private directory paths.
    """
    return not name.startswith('.') and name not in STATE_FILENAMES
