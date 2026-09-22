"""Shared public/private sensitivity policy for indexed documents.

Single source of the label vocabulary and its normalization so every layer that
touches it — the vector store (``rag_vector``), the directory tracker
(``personal_docs``), the HTTP routes and the agent tool surface — applies the
same rule and cannot drift, the same way ``index_walk`` single-sources the
directory-walk policy.

The label answers exactly one question: *may this chunk leave the machine?*
``private`` chunks are only retrieved when the active chat or external agent
has an explicit private-vault read grant; ``public`` chunks may go to any
model, including hosted APIs. Endpoint location is not authorization.

Unlabeled content is treated as ``public``. Chunks indexed before this feature
existed were already being sent to whatever model the session used, so
defaulting them to ``public`` preserves behaviour instead of silently emptying
an existing index. ``private`` is never inferred — a user has to ask for it.

``resolve_sensitivity`` (below) is the store-agnostic entry point: it lets any vault-backed store — not just PersonalDocsManager's own
directory tracking — answer "public or private?" for a path via one
precedence chain (per-file frontmatter, then the ``vault_folder_sensitivity``
setting, then the legacy per-directory state file, then the configured
default). The file-tool deny-list calls the same resolver for paths inside the
vault, so a folder cannot be private to search while remaining readable by an
agent through ``read_file``.
"""
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

SENSITIVITY_PUBLIC = "public"
SENSITIVITY_PRIVATE = "private"

VALID_SENSITIVITIES = (SENSITIVITY_PUBLIC, SENSITIVITY_PRIVATE)
ACCESS_READONLY = "readonly"

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
            if any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or value.strip().lower() not in VALID_SENSITIVITIES
                for key, value in stored.items()
            ):
                raise ValueError("directory sensitivity contains an invalid path or label")
            _private_dirs_cache["dirs"] = tuple(
                os.path.abspath(key)
                for key, value in stored.items()
                if isinstance(key, str) and is_private(value)
            )
            _private_dirs_cache["mtime"] = mtime
        except Exception as e:
            # Keep the previous set; do not fall open on a partial/corrupt read.
            logger.warning("Could not read %s (%s); keeping previous private set", path, e)
            if not _private_dirs_cache["dirs"]:
                # On the first read there is no last-known-good policy to keep.
                # Treat the active vault as private until the state file can be
                # parsed instead of turning corruption into an authorization.
                return (os.path.realpath(vault_root()),)

    return _private_dirs_cache["dirs"]


def path_is_under_private_directory(path: str, vault_real: Optional[str] = None) -> bool:
    """True when ``path`` is inside (or is) a directory labelled private.

    Path-boundary match, not a prefix match, so a private ``/docs`` does not
    also capture ``/docs2``. ``vault_real`` is the already-resolved vault root,
    passed by callers that check many paths in one walk.
    """
    if not isinstance(path, str) or not path:
        return False
    abs_path = os.path.abspath(path)
    root = vault_real or os.path.realpath(vault_root())
    candidate = os.path.realpath(abs_path)
    try:
        inside_vault = os.path.commonpath([candidate, root]) == root
    except ValueError:
        inside_vault = False
    if inside_vault:
        frontmatter = None
        if os.path.isfile(candidate) and candidate.lower().endswith((".md", ".markdown")):
            try:
                from src.vault_markdown import split_frontmatter

                with open(candidate, "r", encoding="utf-8") as handle:
                    # Frontmatter is bounded and appears first; do not read a whole
                    # large document merely to decide whether file tools may see it.
                    header = handle.read(65537)
                    frontmatter, _ = split_frontmatter(header)
                # An opening header that cannot be parsed within the bound is
                # security metadata we cannot trust. Fail closed instead of
                # silently falling through to a public folder default.
                if header.lstrip("\ufeff").startswith("---") and not frontmatter:
                    return True
            except Exception as exc:
                logger.warning("Could not read vault frontmatter for %s (%s); denying access", candidate, exc)
                return True
        return resolve_sensitivity(candidate, frontmatter=frontmatter) == SENSITIVITY_PRIVATE
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


# --------------------------------------------------------------------------- #
# Store-agnostic policy: resolve_sensitivity
# --------------------------------------------------------------------------- #
#
# Everything below answers "public or private?" for a path that may not even
# live under PERSONAL_DIR any more (``vault_directory`` can point anywhere),
# and may not be a real file at all (a note stored as a DB row still needs a
# label). It reads three settings from ``src.settings`` — ``vault_directory``,
# ``vault_default_sensitivity``, ``vault_folder_sensitivity`` — without ever
# writing them; this module only consumes policy, it does not administer it.


def vault_root() -> str:
    """Root of the document vault.

    ``vault_directory`` empty means "no override yet configured" — every
    existing deployment already has content indexed under PERSONAL_DIR, so
    that is the fallback rather than some new empty default (see the
    DEFAULT_SETTINGS comment in ``src.settings``).
    """
    from src.constants import PERSONAL_DIR
    from src.settings import get_setting

    configured = get_setting("vault_directory", "")
    if isinstance(configured, str) and configured.strip():
        return configured
    return PERSONAL_DIR


def _looks_absolute(path: str) -> bool:
    """True if ``path`` is (or resembles) a real filesystem reference rather
    than an already vault-relative logical path such as ``"Journal/note.md"``.

    ``os.path.isabs`` alone is not enough on Windows: a path with a drive but
    no root (``"C:foo"``) or a leading slash but no drive (``"/etc/passwd"``)
    is *drive-relative*, not absolute by that function's definition, yet both
    are real filesystem references and must not be treated as vault-relative
    text.
    """
    if os.path.isabs(path):
        return True
    if os.path.splitdrive(path)[0]:
        return True
    return path.replace("\\", "/").startswith("/")


def _normalize_vault_relative(raw: str) -> Optional[str]:
    """Normalize a candidate vault-relative folder path, or ``None`` if it
    cannot be trusted as one.

    Used for both ``vault_folder_sensitivity`` keys (admin-declared folders)
    and a caller-supplied logical path with no file on disk, so both go
    through the same paranoid gate before they can participate in matching.
    Rejects anything absolute, drive-qualified, or containing a ``..``
    segment — a folder key is meant to name something *inside* the vault, and
    a traversal segment is exactly how it could be made to mean something
    else.
    """
    if not isinstance(raw, str):
        return None
    # The empty JSON key is the vault root. Whitespace is not: accepting it
    # would create an invisible-looking folder name rather than a root rule.
    if raw == "":
        return ""
    if not raw.strip():
        return None
    if _looks_absolute(raw):
        return None
    parts = [p for p in raw.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    return "/".join(parts)  # "" denotes the vault root itself


def _to_vault_relative(path: str, root: str) -> Optional[str]:
    """Best-effort vault-relative, forward-slash path for folder matching.

    Accepts either a real filesystem path (resolved against ``root``) or a
    purely logical vault-relative path for content with no file on disk.
    Returns ``None`` when the path cannot be placed inside the vault at all —
    a real path outside ``root``, or a logical path that tries to traverse
    out of it — so the caller moves on to the next precedence layer instead
    of matching the wrong folder.
    """
    if not isinstance(path, str) or not path:
        return None
    if _looks_absolute(path):
        root_abs = os.path.realpath(root)
        candidate_abs = os.path.realpath(path)
        try:
            if os.path.commonpath([candidate_abs, root_abs]) != root_abs:
                return None
        except ValueError:  # different drives on Windows
            return None
        rel = os.path.relpath(candidate_abs, root_abs).replace("\\", "/")
        return "" if rel == "." else rel
    return _normalize_vault_relative(path)


@dataclass(frozen=True)
class FolderPolicy:
    """Independent privacy and agent-write rules for a vault folder.

    ``None`` means that property is inherited from the closest ancestor (or
    the vault default). Keeping the properties independent lets a folder be
    both private and read-only without changing the existing sensitivity
    vocabulary used by indexed chunks.
    """

    sensitivity: Optional[str] = None
    readonly: Optional[bool] = None


def _deepest_policy_value(
    folder_map: Dict[str, FolderPolicy], vault_rel: str, attribute: str
) -> Any:
    """Deepest explicitly declared value for one folder-policy property."""
    target = vault_rel.casefold()
    best_value: Any = None
    best_len = -1
    for folder, policy in folder_map.items():
        value = getattr(policy, attribute)
        if value is None:
            continue
        folder_cf = folder.casefold()
        matches = folder_cf == "" or target == folder_cf or target.startswith(folder_cf + "/")
        if matches and len(folder_cf) > best_len:
            best_len = len(folder_cf)
            best_value = value
    return best_value


def _safe_folder_policy_map() -> Tuple[Dict[str, FolderPolicy], bool]:
    """Validate the vault folder policy setting as a whole.

    This is admin-controlled security policy, not user content, so it is
    validated all-or-nothing: a wrong container type, a non-string key or
    value, an unrecognised rule, or a key that tries to escape the vault via
    ``..`` makes the ENTIRE setting untrustworthy for this call rather than
    silently dropping just the one bad entry and matching everything else. A
    corrupt or hand-edited settings.json must not let some paths quietly fall
    through to a public default — the caller treats ``valid=False`` as "fail
    closed to private".

    Returns ``(map, valid)``. ``map`` is only meaningful when ``valid`` is
    ``True``.
    """
    from src.settings import get_setting

    raw = get_setting("vault_folder_sensitivity", {})
    if not isinstance(raw, dict):
        logger.warning(
            "vault_folder_sensitivity is not an object (got %s); failing closed to private",
            type(raw).__name__,
        )
        return {}, False

    result: Dict[str, FolderPolicy] = {}
    normalized_keys: set[str] = set()
    for key, value in raw.items():
        if not isinstance(key, str):
            logger.warning(
                "vault_folder_sensitivity has a non-string key (%r: %r); "
                "failing closed to private",
                key, value,
            )
            return {}, False
        normalized_key = _normalize_vault_relative(key)
        if normalized_key is None:
            logger.warning(
                "vault_folder_sensitivity has an unsafe folder path %r; failing closed to private",
                key,
            )
            return {}, False
        normalized_cf = normalized_key.casefold()
        if normalized_cf in normalized_keys:
            logger.warning(
                "vault_folder_sensitivity has duplicate normalized folder path %r; "
                "failing closed to private",
                key,
            )
            return {}, False
        normalized_keys.add(normalized_cf)
        policy: Optional[FolderPolicy] = None
        if isinstance(value, str):
            label = value.strip().lower()
            if label in VALID_SENSITIVITIES:
                policy = FolderPolicy(sensitivity=label)
            elif label == ACCESS_READONLY:
                policy = FolderPolicy(readonly=True)
        elif isinstance(value, dict):
            unknown = set(value) - {"sensitivity", "readonly"}
            sensitivity = value.get("sensitivity")
            readonly = value.get("readonly")
            has_sensitivity = "sensitivity" in value
            has_readonly = "readonly" in value
            sensitivity_valid = (
                not has_sensitivity
                or (
                    isinstance(sensitivity, str)
                    and sensitivity.strip().lower() in VALID_SENSITIVITIES
                )
            )
            readonly_valid = not has_readonly or isinstance(readonly, bool)
            if not unknown and (has_sensitivity or has_readonly) and sensitivity_valid and readonly_valid:
                policy = FolderPolicy(
                    sensitivity=sensitivity.strip().lower() if has_sensitivity else None,
                    readonly=readonly if has_readonly else None,
                )
        if policy is None:
            logger.warning(
                "vault_folder_sensitivity has an unrecognised policy %r for %r; "
                "failing closed to private",
                value, key,
            )
            return {}, False
        result[normalized_key] = policy

    return result, True


def path_is_readonly(path: str) -> bool:
    """Return whether ``path`` is read-only for an LLM/agent write.

    Read-only rules inherit independently from sensitivity rules. A child can
    explicitly opt back into writes with ``{"readonly": false}``. Paths
    outside the configured vault are unaffected. As with privacy, malformed
    admin policy fails closed for paths inside the vault.
    """
    vault_rel = _to_vault_relative(str(path), vault_root())
    if vault_rel is None:
        return False
    folder_map, config_is_valid = _safe_folder_policy_map()
    if not config_is_valid:
        return True
    return _deepest_policy_value(folder_map, vault_rel, "readonly") is True


class VaultReadOnlyError(PermissionError):
    """Raised when a guarded LLM/agent write targets a read-only vault path."""


def assert_vault_writable(path: str, *, operation: str = "modify") -> None:
    """Raise before an LLM/agent changes a read-only vault path.

    Human-facing routes intentionally bypass this guard. Filesystem
    permissions remain the ultimate limit for those human edits.
    """
    if path_is_readonly(str(path)):
        raise VaultReadOnlyError(
            f"cannot {operation} {path}: its vault folder is configured readonly"
        )


# Second cache over the same legacy state file.  It retains public declarations
# as well as private ones for precedence resolution.
_legacy_state_cache: Dict[str, Any] = {"mtime": None, "map": {}}


def _read_legacy_sensitivity_map() -> Dict[str, str]:
    """Full (public + private) view of the legacy directory_sensitivity.json
    state file, keyed by absolute directory, for resolve_sensitivity's
    precedence layer (c). A missing or corrupt file behaves like
    `private_directories`: missing means no declarations, corrupt keeps the
    last known-good map instead of clearing it.
    """
    from src.constants import PERSONAL_DIR

    path = os.path.join(PERSONAL_DIR, SENSITIVITY_STATE_FILENAME)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _legacy_state_cache["mtime"] = None
        _legacy_state_cache["map"] = {}
        return {}

    if _legacy_state_cache["mtime"] != mtime:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
            if not isinstance(stored, dict):
                raise ValueError("directory sensitivity must be an object")
            if any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or value.strip().lower() not in VALID_SENSITIVITIES
                for key, value in stored.items()
            ):
                raise ValueError("directory sensitivity contains an invalid path or label")
            _legacy_state_cache["map"] = {
                os.path.abspath(key): normalize_sensitivity(value)
                for key, value in stored.items()
                if isinstance(key, str)
            }
            _legacy_state_cache["mtime"] = mtime
        except Exception as e:
            logger.warning("Could not read %s (%s); keeping previous state", path, e)
            if not _legacy_state_cache["map"]:
                # No last-known-good declaration exists yet. A corrupt policy
                # must not silently inherit the public default.
                return {os.path.realpath(vault_root()): SENSITIVITY_PRIVATE}

    return _legacy_state_cache["map"]


def _legacy_sensitivity_for(path: str) -> Optional[str]:
    """Deepest-matching label from the legacy per-directory declarations, or
    ``None`` when nothing in the state file covers ``path``.

    A logical (non-absolute) path is resolved against `vault_root` first —
    the legacy file always stores absolute directories, so there is nothing
    else to compare a bare logical path against.
    """
    if not isinstance(path, str) or not path:
        return None
    if _looks_absolute(path):
        abs_path = os.path.abspath(path)
    else:
        abs_path = os.path.abspath(os.path.join(vault_root(), path))

    best_label: Optional[str] = None
    best_len = -1
    for directory, label in _read_legacy_sensitivity_map().items():
        if abs_path == directory or abs_path.startswith(directory + os.sep):
            if len(directory) > best_len:
                best_len = len(directory)
                best_label = label
    return best_label


def _frontmatter_sensitivity(frontmatter: Optional[Dict[str, Any]]) -> Optional[str]:
    """Explicit per-file override from a note's own frontmatter, or ``None``
    when the key is absent — as opposed to present-but-odd, which still
    counts as explicit (see `normalize_sensitivity`)."""
    if not isinstance(frontmatter, dict):
        return None
    for key, value in frontmatter.items():
        if str(key).lower() == SENSITIVITY_KEY:
            return normalize_sensitivity(value)
    return None


def resolve_sensitivity(path: str, *, frontmatter: Optional[Dict[str, Any]] = None) -> str:
    """Resolve the sensitivity label that applies to ``path``.

    ``path`` may be a real filesystem path (absolute, or resolvable against
    `vault_root`) or a purely logical vault-relative path for content with no
    file on disk — a note stored as a DB row still needs a label from the
    same policy a file on disk would get.

    Precedence, highest first:
      1. An explicit ``sensitivity:`` in the file's OWN frontmatter. A
         statement about this one file beats every folder-wide rule, in
         either direction — a public note inside a private tree, or a
         private note inside a public one.
      2. The deepest ancestor folder declared in the ``vault_folder_sensitivity``
         setting. Deepest match wins so a subfolder can carve out an
         exception without re-declaring every sibling.
      3. The legacy ``directory_sensitivity.json`` state file inside
         PERSONAL_DIR — deployments that labelled directories before
         ``vault_folder_sensitivity`` existed (PersonalDocsManager still
         writes here) keep working.
      4. ``vault_default_sensitivity`` — the label for a folder that declares
         nothing. This is NOT hardcoded to private: an undeclared folder gets
         whatever the operator configured as the default (public, unless they
         changed it).

    ``vault_folder_sensitivity`` is validated as a whole before layer 2 runs
    (see `_safe_folder_sensitivity_map`); if it is malformed, resolution fails
    closed to ``private`` for everything except an explicit frontmatter
    override, rather than risk silently falling through to a public default
    on a corrupted admin setting.
    """
    explicit = _frontmatter_sensitivity(frontmatter)
    if explicit is not None:
        return explicit

    folder_map, config_is_valid = _safe_folder_policy_map()
    if not config_is_valid:
        return SENSITIVITY_PRIVATE

    vault_rel = _to_vault_relative(path, vault_root())
    if vault_rel is not None:
        folder_label = _deepest_policy_value(folder_map, vault_rel, "sensitivity")
        if folder_label is not None:
            return folder_label

    legacy_label = _legacy_sensitivity_for(path)
    if legacy_label is not None:
        return legacy_label

    from src.settings import get_setting

    return normalize_sensitivity(get_setting("vault_default_sensitivity", SENSITIVITY_PUBLIC))
