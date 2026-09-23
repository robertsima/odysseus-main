"""
tool_execution.py

Tool dispatcher and result formatter for the agent loop.
Routes tool blocks to MCP servers or native implementations.

Extracted from agent_tools.py.
"""

import asyncio
import collections
import contextvars
import json
import logging
import os
import pathlib
import re
import stat
import sys
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple



from src.tool_security import (
    BUILTIN_EMAIL_TOOLS,
    email_tool_policy_names,
    is_public_blocked_tool,
    owner_is_admin_or_single_user,
)
from src.tool_capabilities import ToolRunSecurityContext, blocked_tool_result
from src.tool_approvals import ExactToolApproval
from src.tool_policy import ToolPolicy
from src.private_access import effective_private_grant, private_tool_denial, tool_requires_private_grant
from src.constants import (
    MAX_OUTPUT_CHARS,
    MAX_READ_CHARS,
    MAX_DIFF_LINES,
    AGENT_WORKSPACE_DIR,
)
from src.tool_utils import _truncate, get_mcp_manager


class _MissingToolSecurityContext:
    pass


class _NoToolSecurityContext:
    """Explicit sentinel for non-agent callers that have no run provenance."""


_MISSING_TOOL_SECURITY_CONTEXT = _MissingToolSecurityContext()
NO_TOOL_SECURITY_CONTEXT = _NoToolSecurityContext()

# Persistent working directory for agent subprocesses.
# Resolves to <repo_root>/data/agent_workspace, inside the bind-mounted volume
# in Docker (/app/data), so files survive a rebuild as before. The subdirectory
# rather than data/ itself keeps agent scratch files and dotfiles out of the
# directory holding the session store and the auth database.
_AGENT_WORKDIR = AGENT_WORKSPACE_DIR


def _git_safe_directory_env(env: Dict[str, str]) -> Dict[str, str]:
    """Let the agent's git open checkouts under DATA_DIR owned by another uid.

    The container runs as root and the data volume belongs to the host user
    (uid 1000 on ZimaOS). Git 2.35.2+ refuses such a repository as "dubious
    ownership", and commands that can also run outside a repository
    (`ls-remote`) silently carry on as if there were none, so `origin` "does
    not appear to be a git repository". Every git command in every checkout
    under /app/data died that way on 2026-09-11 after a rebuild emptied the
    /root/.gitconfig that used to carry the exception.

    Passed through GIT_CONFIG_* so nothing is written to disk and an operator's
    own GIT_CONFIG_* entries are kept, not clobbered.
    """
    try:
        count = int((env.get("GIT_CONFIG_COUNT") or "0").strip() or 0)
    except ValueError:
        count = 0
    out: Dict[str, str] = {}
    root = _AGENT_WORKDIR.rstrip("/\\") or _AGENT_WORKDIR
    for i, value in enumerate((root, f"{root}/*"), start=count):
        out[f"GIT_CONFIG_KEY_{i}"] = "safe.directory"
        out[f"GIT_CONFIG_VALUE_{i}"] = value
    out["GIT_CONFIG_COUNT"] = str(count + 2)
    return out



# ---------------------------------------------------------------------------
# Path confinement for read_file / write_file
# ---------------------------------------------------------------------------
# read_file + write_file are admin-only tools, but the path the agent
# supplies is model-controlled. Prompt-injection in an admin's chat can
# weaponise "read /etc/shadow" or "write ~/.ssh/authorized_keys" without
# the admin noticing.
#
# Policy:
#   1. Sensitive-subpath deny list — checked FIRST. Blocks .ssh,
#      .gnupg, shell rc files, token/env files even if the root above
#      them is on the allowlist.
#   2. Application-state deny (_is_app_state_path) - DATA_DIR holds the
#      session store, auth database, app key and settings, so only
#      _agent_readable_data_subdirs() is readable inside it.
#   3. Allowlist - only the directories the agent legitimately needs
#      (its data/ workspace, user content, system tmp). $HOME is NOT on
#      the default list.
#   4. Opt-in extra roots - admin can add broader roots via the
#      "tool_path_extra_roots" setting. These cannot re-open DATA_DIR;
#      rule 2 is independent of which root a path arrived through.
# ---------------------------------------------------------------------------

_SENSITIVE_BASENAMES: set[str] = {
    ".ssh", ".gnupg", ".gitconfig",
    ".bashrc", ".bash_profile", ".bash_logout",
    ".zshrc", ".zprofile", ".zshenv",
    ".profile", ".tcshrc", ".cshrc",
    ".env", ".netrc",
    # `git credential-store` keeps https://user:token@host lines here.
    ".git-credentials",
}

_SENSITIVE_FILE_PATTERNS: tuple[str, ...] = (
    "authorized_keys", "id_rsa", "id_ed25519", "id_ecdsa",
    "known_hosts",
)

# Private-key file extensions. In a container the only reliably writable path is
# the bind-mounted data dir, which is also an allowed tool root — so a GitHub App
# key, a TLS key, or a signing key parked there would otherwise be readable by
# the agent's own read_file. Reading the App key is equivalent to holding the
# credential, which would let the agent mint its own installation tokens and walk
# around the human approval gate entirely.
_SENSITIVE_KEY_SUFFIXES: tuple[str, ...] = (
    ".pem", ".key", ".p12", ".pfx", ".jks", ".der", ".pkcs12", ".asc",
)

# Case-folded views used for matching. On a case-insensitive filesystem
# (Windows, default macOS) ".SSH/AUTHORIZED_KEYS" and ".env" resolve to the
# same protected files as their lowercase forms, so the deny-list has to fold
# case before comparing — the sibling resolver already normcases paths for the
# same reason. casefold (not os.path.normcase) because normcase is a no-op on
# POSIX, which is exactly where the macOS read-exfil path lives.
_ENV_TEMPLATE_NAMES_CF: frozenset[str] = frozenset({
    ".env.example", ".env.sample", ".env.template", ".env.dist", ".env.defaults",
})
_SENSITIVE_BASENAMES_CF: frozenset[str] = frozenset(b.casefold() for b in _SENSITIVE_BASENAMES)
_SENSITIVE_FILE_PATTERNS_CF: frozenset[str] = frozenset(p.casefold() for p in _SENSITIVE_FILE_PATTERNS)
_SENSITIVE_KEY_SUFFIXES_CF: tuple[str, ...] = tuple(s.casefold() for s in _SENSITIVE_KEY_SUFFIXES)


# ── walk-scoped policy snapshot ────────────────────────────────────────────
# A directory walk (glob, grep, ls) checks every directory and file against the
# path policy. The policy's inputs come from config: the agent-worktree config
# file (read and parsed twice per check), settings lookups, and realpath calls
# on the same few roots. On a large tree that was ~2 ms per check locally and
# far more on the server's overlay filesystem, where two globs over /app took
# 97 s each. Inside a snapshot those inputs are computed once for the walk;
# outside one (every single-path check) nothing is cached, so no decision is
# ever made on stale config across calls.
_POLICY_SNAPSHOT: contextvars.ContextVar = contextvars.ContextVar("_tool_policy_snapshot", default=None)


def _policy_memo(key: str, compute):
    memo = _POLICY_SNAPSHOT.get()
    if memo is None:
        return compute()
    if key not in memo:
        memo[key] = compute()
    return memo[key]


def run_with_policy_snapshot(fn, *args, **kwargs):
    """Call ``fn`` with the path-policy inputs computed once for its duration.
    For the worker function of a directory walk (runs inside to_thread)."""
    token = _POLICY_SNAPSHOT.set({})
    try:
        return fn(*args, **kwargs)
    finally:
        _POLICY_SNAPSHOT.reset(token)


def _vault_realpath() -> Optional[str]:
    def compute():
        from src.rag_sensitivity import vault_root
        try:
            return os.path.realpath(vault_root())
        except (OSError, ValueError):
            return None
    return _policy_memo("vault_realpath", compute)


def _worktree_config_paths() -> tuple:
    """(approval state dir, configured signing-key path), both canonical."""
    def compute():
        try:
            from src.agent_worktree.config import load_config
            cfg = load_config()
        except Exception:
            return (None, None)
        state = None
        key = None
        try:
            state = os.path.normcase(os.path.realpath(cfg.state_dir))
        except Exception:
            state = None
        try:
            if cfg.private_key_path:
                key = os.path.normcase(os.path.realpath(cfg.private_key_path))
        except Exception:
            key = None
        return (state, key)
    return _policy_memo("worktree_config", compute)


def _is_sensitive_path(resolved: str, allow_private: bool = False) -> bool:
    """Return True if *resolved* falls under a sensitive directory or
    matches a sensitive filename — regardless of what root it sits under.

    Matching is case-insensitive: on Windows / default macOS a case-variant
    name (``.SSH``, ``AUTHORIZED_KEYS``, ``Id_Rsa``) points at the same file as
    the lowercase form, so a case-sensitive check would let it slip past the
    deny-list in every file tool that relies on it.
    """
    # Accept both separators so tests and model-supplied paths copied from a
    # different platform cannot evade the deny-list on Windows.
    parts = [p.casefold() for p in re.split(r"[\\\\/]", resolved)]
    filename = parts[-1] if parts else ""

    # Check if any path component is a sensitive directory.
    for part in parts:
        if part in _SENSITIVE_BASENAMES_CF:
            return True

    # A repository's .git/config can carry a token in a remote URL
    # (https://x-access-token:<token>@github.com/...) or an http extraheader,
    # and writing it can set hooks/fsmonitor commands. manage_git's `remotes`
    # action reports remotes credential-free; the raw file stays off limits.
    if len(parts) >= 2 and filename == "config" and parts[-2] == ".git":
        return True

    # `.env` is listed above; its per-environment variants (.env.local,
    # .env.production, ...) hold the same secrets. Committed templates carry
    # placeholders only and are what an agent needs to learn the variables.
    if filename.startswith(".env.") and filename not in _ENV_TEMPLATE_NAMES_CF:
        return True

    # Documents the user labelled private. These live under PERSONAL_DIR, which
    # sits inside DATA_DIR — an allowed tool root — so without this check any
    # model could read a private note by absolute path and walk straight around
    # the RAG sensitivity filter. Blocking here covers the file tools only:
    # indexing and retrieval read from disk directly and never consult this
    # function. An explicit per-chat grant is required for both retrieval and
    # direct reads; merely using a local endpoint does not widen access.
    try:
        from src.rag_sensitivity import (
            path_is_under_private_directory,
            resolve_sensitivity,
            vault_root,
        )

        in_vault = False
        vault = None
        try:
            vault = _vault_realpath()
            in_vault = bool(vault) and (resolved == vault or os.path.commonpath([resolved, vault]) == vault)
        except (OSError, ValueError):
            pass
        if not allow_private and (
            path_is_under_private_directory(resolved, vault_real=vault)
            or (in_vault and resolve_sensitivity(resolved) == "private")
        ):
            return True
    except Exception as e:  # never let a label lookup break file tools
        logger.warning("private-path check failed for %s: %s", resolved, e)

    # The agent worktree's approval state is the trust anchor for human-gated
    # publishing: the records say which change a human approved and carry the
    # key that authenticates them. It lives under DATA_DIR, which IS an allowed
    # tool root, so without this the agent could write itself a "granted"
    # record with its own code hash and publish without a human. Deny-listed
    # here so write_file / edit_file / apply_patch / read_file all refuse it.
    if _is_under_agent_worktree_state(resolved):
        return True

    # Private key material, by extension and by configured location.
    if filename.endswith(_SENSITIVE_KEY_SUFFIXES_CF):
        return True
    if _is_configured_signing_key(resolved):
        return True

    # Check filename against known sensitive files.
    return filename in _SENSITIVE_FILE_PATTERNS_CF


def _is_configured_signing_key(resolved: str) -> bool:
    """True for the GitHub App private key, whatever the operator named it."""
    configured = _worktree_config_paths()[1]
    if not configured:
        return False
    try:
        return configured == os.path.normcase(resolved)
    except (OSError, ValueError):
        return False


def _is_under_agent_worktree_state(resolved: str) -> bool:
    """True when *resolved* sits in the agent worktree's approval state dir."""
    b = _worktree_config_paths()[0]
    if not b:
        return False
    a = os.path.normcase(resolved)
    return a == b or a.startswith(b + os.sep)


# Extra roots declared by the DEPLOYMENT rather than by post-boot admin state.
# `tool_path_extra_roots` (below) is only reachable through the settings API,
# so a compose file that bind-mounts a tree for the agent to work in came up
# with the mount present and every path under it rejected, and the only way
# through was to remember to bind that folder as the workspace by hand — which
# then revoked every other path. Same reasoning as ODYSSEUS_PERSONAL_DIRS: the
# declaration belongs next to the mount. Separator is the platform's path
# separator (":" on POSIX, ";" on Windows); commas are also accepted because
# every other Odysseus list variable uses them.
TOOL_EXTRA_ROOTS_ENV = "ODYSSEUS_TOOL_EXTRA_ROOTS"


def _env_extra_roots() -> list[str]:
    raw = os.environ.get(TOOL_EXTRA_ROOTS_ENV, "")
    if not raw.strip():
        return []
    parts = [raw]
    for sep in (os.pathsep, ","):
        parts = [piece for chunk in parts for piece in chunk.split(sep)]
    return [p.strip() for p in parts if p.strip()]


def _personal_docs_root() -> Optional[str]:
    """Realpath of the user's indexed knowledge base, or None."""
    try:
        from src.constants import PERSONAL_DIR

        return os.path.realpath(PERSONAL_DIR)
    except Exception:
        return None


def _personal_docs_suggestion(raw_path: str) -> str:
    """Point a rejected path at the knowledge base when it names a vault folder.

    Seen live: the agent wrote to `/app/workspace/AI Mind/Projects/Note.md` —
    the vault folder name was right, the root was invented — and after `ls`
    and `write_file` both refused it, it wrote the note through the shell
    instead, outside the vault. When a component of the rejected path is a
    top-level folder of the personal-documents tree, return a sentence naming
    the real path so the next call can succeed. Empty when nothing matches.
    """
    root = _personal_docs_root()
    if not root or not raw_path:
        return ""
    try:
        top = {entry.casefold(): entry for entry in os.listdir(root) if not entry.startswith(".")}
    except OSError:
        return ""
    if not top:
        return ""
    parts = [p for p in re.split(r"[\\/]+", os.path.expanduser(str(raw_path).strip())) if p]
    for i, part in enumerate(parts):
        real = top.get(part.casefold())
        if real is not None:
            suggestion = os.path.join(root, real, *parts[i + 1:])
            return (
                f". The knowledge base (vault) lives under '{root}', not there — "
                f"did you mean '{suggestion}'?"
            )
    return ""


def _is_under_personal_docs(resolved: str) -> bool:
    """True when *resolved* sits inside the personal-documents tree.

    Path-boundary match, not a prefix match, so a sibling `personal_docs2`
    does not count as inside.
    """
    root = _personal_docs_root()
    if not root:
        return False
    a = os.path.normcase(resolved)
    b = os.path.normcase(root)
    return a == b or a.startswith(b + os.sep)


def _path_within(resolved: str, root: str) -> bool:
    """True when *resolved* is *root* itself or sits underneath it.

    Use the platform's path-case rules.  This helper participates in allow
    decisions, so unconditional case-folding would let a distinct ``/DATA``
    tree masquerade as a descendant of ``/data`` on case-sensitive systems.
    """
    resolved, root = os.path.normcase(resolved), os.path.normcase(root)
    if resolved == root:
        return True
    try:
        if os.path.commonpath([resolved, root]) == root:
            return True
    except ValueError:
        return False
    # normcase is intentionally conservative about assumptions (notably on
    # POSIX), so consult the filesystem when paths exist.  This recognizes a
    # case alias on a case-insensitive volume without treating distinct
    # case-sensitive paths as the same allow root.
    if os.path.exists(root):
        candidate = resolved
        while True:
            try:
                if os.path.exists(candidate) and os.path.samefile(candidate, root):
                    return True
            except OSError:
                pass
            parent = os.path.dirname(candidate)
            if parent == candidate:
                break
            candidate = parent
    return False


def _path_within_conservative(resolved: str, root: str) -> bool:
    """Containment for deny decisions, folding case to fail closed."""
    resolved, root = resolved.casefold(), root.casefold()
    if resolved == root:
        return True
    try:
        return os.path.commonpath([resolved, root]) == root
    except ValueError:
        return False


def _agent_readable_data_subdirs() -> tuple[str, ...]:
    return _policy_memo("readable_data_subdirs", _agent_readable_data_subdirs_uncached)


def _agent_readable_data_subdirs_uncached() -> tuple[str, ...]:
    """The only parts of DATA_DIR the agent's file tools may reach.

    The agent's own scratch folder, plus the directories of user content whose
    paths the application itself gives to the model, which it would then be
    unable to open.  These normally live under DATA_DIR; the documented mail
    attachment override may instead name a disjoint external directory:

      UPLOAD_DIR            the chat upload manifest renders "path=<p>" and
                            says to read it with read_file (agent_loop.py)
      MAIL_ATTACHMENTS_DIR  download_attachment returns the path and its own
                            description tells the model to read it
      PERSONAL_DIR          GET /api/personal returns a path per file and is
                            reachable through the app_api tool; RUNBOOK_DIR
                            nests under it
      PERSONAL_UPLOADS_DIR  indexed as a personal-docs directory, which
                            manage_rag lists as an absolute path

    Order matters: the first entry is roots[0], which _resolve_search_root uses
    when grep/glob/ls are called with no path.
    """
    from src.constants import (
        DATA_DIR,
        MAIL_ATTACHMENTS_DIR,
        PERSONAL_DIR,
        PERSONAL_UPLOADS_DIR,
        UPLOAD_DIR,
    )
    configured = (
        (AGENT_WORKSPACE_DIR, "agent_workspace", False),
        (UPLOAD_DIR, "uploads", False),
        # This has a documented environment override and may legitimately
        # live outside DATA_DIR, but it must never equal/contain DATA_DIR.
        (MAIL_ATTACHMENTS_DIR, "mail-attachments", True),
        (PERSONAL_DIR, "personal_docs", False),
        (PERSONAL_UPLOADS_DIR, "personal_uploads", False),
    )
    configured_data_dir = os.path.abspath(os.path.expanduser(str(DATA_DIR)))
    data_dir = os.path.realpath(configured_data_dir)
    safe: list[str] = []
    for raw, internal_name, external_ok in configured:
        value = str(raw or "").strip()
        # These paths are security-policy roots, not ordinary allowlist
        # entries. Internal roles may inherit a relative DATA_DIR, but must
        # still resolve to their exact canonical child below. External mail
        # overrides require an absolute, disjoint directory.
        if not value:
            continue
        expanded = os.path.abspath(os.path.expanduser(value))
        # A policy root must not acquire an exemption by redirecting its final
        # path component to protected state or to an unrelated external tree.
        if os.path.islink(expanded):
            continue
        resolved = os.path.realpath(expanded)
        if os.path.exists(resolved) and not os.path.isdir(resolved):
            continue
        expected_internal = os.path.join(data_dir, internal_name)
        expected_configured = os.path.join(configured_data_dir, internal_name)
        inside_data = (
            os.path.normcase(expanded)
            in {
                os.path.normcase(expected_configured),
                os.path.normcase(expected_internal),
            }
            and resolved == expected_internal
        )
        external_safe = (
            external_ok
            and os.path.isabs(os.path.expanduser(value))
            and resolved != data_dir
            and os.path.dirname(resolved) != resolved
            and not _path_within(data_dir, resolved)
            and not _path_within(resolved, data_dir)
        )
        if not (inside_data or external_safe) or _is_sensitive_path(resolved):
            continue
        safe.append(resolved)
    safe.extend(r for r in _repository_data_subdirs(data_dir) if r not in safe)
    return tuple(safe)


# Settings-derived, and consulted on every path check; a few seconds is fresh
# enough for a changed repository-roots setting.
_REPO_SUBDIRS_TTL_S = 5.0
_REPO_SUBDIRS_CACHE: dict[str, tuple[float, tuple[str, ...]]] = {}


def _repository_data_subdirs(data_dir: str) -> tuple[str, ...]:
    """Configured repository roots that live inside DATA_DIR.

    The defaults (/app/data/development and /app/data/agent_worktrees) are
    under DATA_DIR, and get_workspace lists the checkouts in them, yet every
    file tool refused them as application state and a workspace could not be
    bound there: the 2026-09-18 workers reported "cannot read
    /app/data/development/odysseus-main" and stopped. They are admitted here
    only as real, non-symlinked directories strictly inside DATA_DIR that hold
    neither DATA_DIR itself nor the worktree approval state. Sensitive files,
    private documents and that approval state are still refused inside them,
    by the checks in _is_sensitive_path.
    """
    now = time.monotonic()
    cached = _REPO_SUBDIRS_CACHE.get(data_dir)
    if cached and now - cached[0] < _REPO_SUBDIRS_TTL_S:
        return cached[1]
    try:
        from src.agent_worktree.config import load_config
        from src.agent_worktree.repository_sync import git_repository_roots

        roots = [str(root) for root in git_repository_roots()]
        state_dir = os.path.realpath(load_config().state_dir)
    except Exception:
        return ()
    out: list[str] = []
    for raw in roots:
        expanded = os.path.abspath(os.path.expanduser(raw))
        if os.path.islink(expanded):
            continue
        resolved = os.path.realpath(expanded)
        if (
            not os.path.isdir(resolved)
            or resolved == data_dir
            or not _path_within(resolved, data_dir)
            or _path_within(state_dir, resolved)
            or _is_sensitive_path(resolved)
        ):
            continue
        out.append(resolved)
    result = tuple(out)
    _REPO_SUBDIRS_CACHE[data_dir] = (now, result)
    return result


def _is_app_state_path(resolved: str) -> bool:
    """True for anything under DATA_DIR that is not agent-readable.

    DATA_DIR holds the session store, the auth database, the app encryption key
    and the settings file. A model-supplied path must not reach those through
    any root, so this is checked in both resolvers rather than expressed as an
    absence from the allowlist: a workspace bound at or above the data
    directory, or an opt-in tool_path_extra_roots entry covering it, would
    otherwise put them back in reach.

    A containment rule rather than a filename deny list, so state files added
    later are covered without anyone remembering to list them, and so a user's
    own settings.json or app.db inside a real workspace is not caught.
    """
    from src.constants import DATA_DIR
    data_real = _policy_memo("data_dir_realpath", lambda: os.path.realpath(DATA_DIR))
    if not _path_within_conservative(resolved, data_real):
        return False
    return not any(
        _path_within(resolved, d)
        for d in _agent_readable_data_subdirs()
    )


def _is_hardlinked_regular_file(resolved: str) -> bool:
    """Reject inode aliases that can smuggle DATA_DIR state into an allow root."""
    try:
        target = os.stat(resolved, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(target.st_mode) and getattr(target, "st_nlink", 1) > 1


def _is_denied_tool_path(resolved: str, allow_private: bool = False) -> bool:
    """Apply every path deny to a canonical traversal result.

    ``allow_private`` is the chat's effective private-vault grant; without it
    documents labelled private are denied like any other sensitive path.
    """
    return (
        _is_sensitive_path(resolved, allow_private=allow_private)
        or _is_app_state_path(resolved)
        or _is_hardlinked_regular_file(resolved)
    )


def _can_traverse_tool_path(resolved: str, allow_private: bool = False) -> bool:
    """Allow walking a denied state parent only to reach safe carve-outs."""
    if _is_sensitive_path(resolved, allow_private=allow_private):
        return False
    if not _is_app_state_path(resolved):
        return True
    return any(
        _path_within(readable, resolved)
        for readable in _agent_readable_data_subdirs()
    )


def _tool_path_roots() -> list[str]:
    """Return the list of directory roots that read_file / write_file
    may touch. Default: project data/ + system temp dirs. Extra roots
    are loaded from the ``tool_path_extra_roots`` setting and from
    ``ODYSSEUS_TOOL_EXTRA_ROOTS``.
    """
    roots: list[str] = []

    # The agent's workspace plus the user-content directories inside data/.
    # The rest of DATA_DIR is denied by _is_app_state_path.
    roots.extend(_agent_readable_data_subdirs())

    # /tmp (and its macOS realpath /private/tmp).
    roots.append("/tmp")
    try:
        private_tmp = os.path.realpath("/tmp")
        if private_tmp != "/tmp":
            roots.append(private_tmp)
    except OSError:
        pass

    # $TMPDIR — per-user temp root on macOS (e.g. /var/folders/.../T/).
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        roots.append(tmpdir)

    # Opt-in extra roots from settings.
    try:
        from src.settings import get_setting
        extra = get_setting("tool_path_extra_roots")
        if isinstance(extra, list):
            roots.extend(str(r) for r in extra if r)
    except Exception:
        pass

    # Opt-in extra roots declared by the deployment.
    roots.extend(_env_extra_roots())

    # Deduplicate; resolve symlinks so containment is unambiguous.
    seen: set[str] = set()
    out: list[str] = []
    for r in roots:
        try:
            real = os.path.realpath(r)
        except OSError:
            continue
        if real in seen:
            continue
        seen.add(real)
        out.append(real)
    return out


def _resolve_tool_path(raw_path: str, allow_private: bool = False) -> str:
    """Resolve and confine a model-supplied path.

    Order of checks:
      1. Non-empty path.
      2. Sensitive-subpath deny list (blocks .ssh, .gnupg, etc.
         even when the root is on the allowlist).
      3. Allowlist containment (must land under one of the roots).

    Returns the realpath on success. Raises ValueError on rejection.
    Symlinks are resolved before comparison.

    When a workspace is active for this turn, paths are confined to it
    (see _resolve_tool_path_in_workspace) PLUS the personal-documents tree.
    """
    ws = get_active_workspace()
    if ws:
        try:
            return _resolve_tool_path_in_workspace(ws, raw_path, allow_private=allow_private)
        except ValueError as workspace_error:
            # Only a path that is merely outside the workspace gets the second
            # chance below. A path the workspace resolver DENIED (sensitive,
            # application state, hard-linked) keeps that refusal: re-resolving
            # it elsewhere would mask the reason, or quietly hand back a
            # different file of the same name.
            if "is outside the workspace" not in str(workspace_error):
                raise
            # The knowledge base is not "somewhere else on the host" — it is
            # the user's own indexed notes, reachable by these same tools when
            # no workspace is bound, and the paths `search_documents` cites.
            # Binding a workspace used to revoke that, so retrieval handed the
            # agent a path its own file tools then refused, and the vault could
            # only be edited by binding the vault *as* the workspace — which in
            # turn revoked everything else. Keep it reachable in both modes.
            # The sensitive/private deny-list below still applies, so a
            # directory the user labelled private stays closed either way.
            return _resolve_personal_docs_path(raw_path, allow_private=allow_private)
    if raw_path is None or not str(raw_path).strip():
        raise ValueError("path is required")
    expanded = os.path.expanduser(str(raw_path).strip())
    resolved = os.path.realpath(expanded)

    if _is_sensitive_path(resolved, allow_private=allow_private):
        raise ValueError(
            f"path '{raw_path}' is inside a sensitive directory "
            f"(e.g. .ssh, .gnupg) or matches a sensitive filename"
        )
    if _is_app_state_path(resolved):
        raise ValueError(
            f"path '{raw_path}' is inside the application state directory"
        )
    if _is_hardlinked_regular_file(resolved):
        raise ValueError(f"path '{raw_path}' is a hard-linked file")

    for root in _tool_path_roots():
        if resolved == root:
            return resolved
        try:
            common = os.path.commonpath([resolved, root])
        except ValueError:
            continue
        if common == root:
            return resolved
    raise ValueError(
        f"path '{raw_path}' is outside the allowed roots" + _personal_docs_suggestion(raw_path)
    )


def _resolve_personal_docs_path(raw_path: str, allow_private: bool = False) -> str:
    """Resolve a path that must land inside the personal-documents tree.

    Used as the second chance when a workspace is bound: the workspace is the
    primary root, the knowledge base is the one root that is always also in
    reach. Raises ValueError with the workspace-style message when the path is
    neither, so the agent sees one coherent rejection rather than two.
    """
    if raw_path is None or not str(raw_path).strip():
        raise ValueError("path is required")
    root = _personal_docs_root()
    expanded = os.path.expanduser(str(raw_path).strip())
    candidate = expanded
    if not os.path.isabs(candidate) and root:
        candidate = os.path.join(root, candidate)
    resolved = os.path.realpath(candidate)
    if _is_sensitive_path(resolved, allow_private=allow_private):
        raise ValueError(
            f"path '{raw_path}' is inside a sensitive directory "
            f"(e.g. .ssh, .gnupg) or matches a sensitive filename"
        )
    if _is_app_state_path(resolved):
        raise ValueError(
            f"path '{raw_path}' is inside the application state directory"
        )
    if _is_hardlinked_regular_file(resolved):
        raise ValueError(f"path '{raw_path}' is a hard-linked file")
    if not _is_under_personal_docs(resolved):
        raise ValueError(
            f"path '{raw_path}' is outside the workspace and outside the "
            f"personal documents directory" + _personal_docs_suggestion(raw_path)
        )
    return resolved


def _resolve_tool_path_in_workspace(workspace: str, raw_path: str, allow_private: bool = False) -> str:
    """Confine a model-supplied path to the active workspace.

    Layered on top of upstream's path policy: the workspace is the allowed
    root (relative paths resolve under it; paths that escape it are rejected),
    and the sensitive-file deny list (.ssh, .gnupg, id_rsa, …) still applies
    inside it. When no workspace is set, callers use _resolve_tool_path (the
    default data/tmp allowlist) instead.
    """
    if raw_path is None or not str(raw_path).strip():
        raise ValueError("path is required")
    base = os.path.realpath(workspace)
    expanded = os.path.expanduser(str(raw_path).strip())
    candidate = expanded if os.path.isabs(expanded) else os.path.join(base, expanded)
    resolved = os.path.realpath(candidate)
    if _is_sensitive_path(resolved, allow_private=allow_private):
        raise ValueError(
            f"path '{raw_path}' is inside a sensitive directory "
            f"(e.g. .ssh, .gnupg) or matches a sensitive filename"
        )
    if _is_app_state_path(resolved):
        raise ValueError(
            f"path '{raw_path}' is inside the application state directory"
        )
    if _is_hardlinked_regular_file(resolved):
        raise ValueError(f"path '{raw_path}' is a hard-linked file")
    if resolved != base:
        # normcase so containment holds on case-insensitive filesystems
        # (Windows, default macOS): it lowercases on Windows and is a no-op on
        # POSIX. commonpath raises ValueError across Windows drives (C: vs D:)
        # or mixed abs/rel — both mean "outside", so the except rejects them.
        nbase = os.path.normcase(base)
        try:
            if os.path.commonpath([os.path.normcase(resolved), nbase]) != nbase:
                raise ValueError
        except ValueError:
            raise ValueError(f"path '{raw_path}' is outside the workspace ({workspace})")
    return resolved



# ---------------------------------------------------------------------------
# Active workspace (per-turn, context-local)
# ---------------------------------------------------------------------------
# Set ONCE in execute_tool_block from the request's `workspace`. The path
# resolvers (_resolve_tool_path / _resolve_search_root) and the subprocess cwd
# helper (agent_cwd) read it from here, so confinement is enforced in a single
# place: any tool that resolves paths through these helpers is confined
# automatically and cannot accidentally bypass the workspace. contextvars are
# task-local, so concurrent turns don't leak into each other.
_active_workspace: contextvars.ContextVar = contextvars.ContextVar(
    "agent_active_workspace", default=None
)


# Set by the private-grant gate when bash/python may run without the grant
# because src.shell_sandbox confines them to this workspace. The handlers
# read it; None means "run as before" (grant present) or "refused".
_shell_sandbox_workspace: contextvars.ContextVar = contextvars.ContextVar(
    "agent_shell_sandbox_workspace", default=None
)


def get_shell_sandbox_workspace() -> Optional[str]:
    """The workspace bash/python must be sandboxed to for this call, if any."""
    return _shell_sandbox_workspace.get()


def get_active_workspace() -> Optional[str]:
    """The folder the agent is confined to this turn, or None."""
    return _active_workspace.get()


def vet_workspace(raw: str) -> Optional[str]:
    """Validate a requested workspace path at bind time.

    Returns the canonical path, or None when it is unusable: not a real
    directory, or itself a sensitive path (.ssh, .gnupg, ...). The in-workspace
    resolver deny-lists sensitive paths *inside* the workspace, but the
    empty-path search root is the workspace itself, so the root has to be
    vetted before it is ever bound.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    resolved = os.path.realpath(os.path.expanduser(raw))
    if not os.path.isdir(resolved) or _is_sensitive_path(resolved):
        return None
    # Refuse the bind rather than binding a workspace where every subsequent
    # tool call would fail on the same deny list.
    if _is_app_state_path(resolved):
        return None
    # Reject filesystem roots: binding / (or a Windows drive/UNC root) as the
    # workspace would make every absolute path "inside" it, collapsing the
    # confinement into host-wide file access. A root is its own dirname, which
    # also covers C:\ and \\server\share without platform-specific lists.
    if os.path.dirname(resolved) == resolved:
        return None
    return resolved


def agent_cwd() -> str:
    """Working directory for agent subprocesses (bash/python/background jobs):
    the active workspace when set, else the persistent data dir."""
    workspace = get_active_workspace()
    if workspace:
        return workspace
    resolved = os.path.realpath(_AGENT_WORKDIR)
    if resolved not in _agent_readable_data_subdirs():
        raise RuntimeError("agent workspace is not a safe real directory")
    return resolved


def get_mcp_manager():
    from src import agent_tools
    return agent_tools.get_mcp_manager()




def _resolve_search_root(raw_path: str, allow_private: bool = False) -> str:
    """Resolve + confine a code-nav path (grep/glob/ls).

    With a workspace active, the workspace folder is the default root and a
    supplied path is confined inside it (or inside the personal-documents tree
    — see _resolve_tool_path, so grepping the vault does not require unbinding
    the workspace). Otherwise an empty path defaults to the agent's primary
    root (its workspace under the project data dir) and a supplied path is
    confined by the global allowlist + sensitive-file policy.
    """
    raw = (raw_path or "").strip()
    ws = get_active_workspace()
    if ws:
        # Resolve the empty case as the workspace path rather than returning
        # it directly: returned unchecked it skipped both deny lists, so a
        # bare ls listed whatever the workspace was bound to.
        if not raw:
            return _resolve_tool_path_in_workspace(ws, ws, allow_private=allow_private)
        return _resolve_tool_path(raw, allow_private=allow_private)
    if not raw:
        roots = _tool_path_roots()
        default_root = os.path.realpath(AGENT_WORKSPACE_DIR)
        if default_root in roots and not _is_denied_tool_path(default_root):
            return default_root
        raise ValueError("default agent workspace is not a safe readable data subdirectory")
    return _resolve_tool_path(raw, allow_private=allow_private)

logger = logging.getLogger(__name__)


_ADMIN_TOOLS = {
    "app_api",
    # Touches the operator's git checkout and the publishing flow; log content
    # is operator-facing diagnostic data.
    "manage_agent_worktree",
    "manage_git",
    "read_app_logs",
    # Runs an external coding agent against an approved repo checkout.
    "delegate_to_agent", "delegate_to_claude_code",
    "manage_endpoints",
    "manage_mcp",
    "manage_webhooks",
    "manage_tokens",
    "manage_settings",
    "download_model",
    "serve_model",
    "serve_preset",
    "stop_served_model",
    "cancel_download",
}


def _owner_is_admin(owner: Optional[str]) -> bool:
    """Mirror route-level admin behavior for agent tool execution."""
    return owner_is_admin_or_single_user(owner)

# ---------------------------------------------------------------------------
# MCP-backed tool helpers
# ---------------------------------------------------------------------------

# Map legacy tool names -> (MCP server_id, MCP tool_name)
_MCP_TOOL_MAP = {
    "bash":           ("bash",       "bash"),
    "python":         ("python",     "python"),
    "read_file":      ("filesystem", "read_file"),
    "write_file":     ("filesystem", "write_file"),
    "web_search":     ("web_search", "web_search"),
    "web_fetch":      ("web_fetch",  "web_fetch"),
    "generate_image": ("image_gen",  "generate_image"),
}
_EMAIL_MCP_OWNER_ARG = "_odysseus_owner"
_LOTUS_MCP_OWNER_ARG = "_odysseus_owner"


def _parse_qualified_mcp_args(tool: str, content: str) -> tuple[Dict, Optional[str]]:
    raw = (content or "").strip()
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        if tool.startswith(("mcp__email__", "mcp__lotus__")):
            return {}, "Owner-scoped MCP tool arguments must be a JSON object."
        return {}, None
    if not isinstance(parsed, dict):
        if tool.startswith(("mcp__email__", "mcp__lotus__")):
            return {}, "Owner-scoped MCP tool arguments must be a JSON object."
        return {}, None
    return parsed, None


def _parse_generate_image(content: str) -> Dict:
    lines = content.strip().split("\n")
    args = {"prompt": lines[0].strip() if lines else ""}
    for i, key in enumerate(["model", "size", "quality"], 1):
        if len(lines) > i and lines[i].strip():
            args[key] = lines[i].strip()
    return args


def _parse_manage_memory(content: str) -> Dict:
    lines = content.strip().split("\n")
    action = lines[0].strip().lower() if lines else ""
    args = {"action": action}
    if action == "add":
        args["text"] = lines[1].strip() if len(lines) > 1 else ""
        if len(lines) > 2 and lines[2].strip():
            args["category"] = lines[2].strip().lower()
    elif action == "edit":
        args["memory_id"] = lines[1].strip() if len(lines) > 1 else ""
        args["text"] = lines[2].strip() if len(lines) > 2 else ""
    elif action == "delete":
        args["memory_id"] = lines[1].strip() if len(lines) > 1 else ""
    elif action == "search":
        args["text"] = lines[1].strip() if len(lines) > 1 else ""
    elif action == "list":
        if len(lines) > 1 and lines[1].strip():
            args["category"] = lines[1].strip().lower()
    return args


def _parse_write_file(content: str) -> Dict:
    lines = content.split("\n", 1)
    return {"path": lines[0].strip(), "content": lines[1] if len(lines) > 1 else ""}


_MCP_ARG_PARSERS: Dict[str, Callable[[str], Dict[str, str]]] = {
    "bash":           lambda c: {"command": c},
    "python":         lambda c: {"code": c},
    "web_search":     lambda c: {"query": c.split("\n")[0].strip()},
    "web_fetch":      lambda c: {"url": c.split("\n")[0].strip()},
    "read_file":      lambda c: {"path": c.split("\n")[0].strip()},
    "write_file":     _parse_write_file,
    "generate_image": _parse_generate_image,
    "manage_memory":  _parse_manage_memory,
}

# Primary argument key(s) for the legacy line-parsed tools. When a fenced
# block's content is a JSON object carrying one of these keys, it's structured
# inline args (the relaxed parser's ```web_search {"query": "..."}``` shape) —
# use the object directly instead of letting the line-based parsers wrap the
# whole JSON string as the query/url/path/prompt. Keyed off membership only
# (the primary key never changes), so this can't drift; an unrecognized object
# safely falls through to the line-based parser, i.e. the previous behavior.
#
# IMPORTANT — this only covers the MCP path. _build_mcp_args is reached via
# _call_mcp_tool only for _MCP_TOOL_MAP tools (so an entry outside that map is
# dead, as manage_memory was). And of these, only generate_image has a live MCP
# server today; web_search/web_fetch/read_file/write_file have none, so they run
# via _direct_fallback -> TOOL_HANDLERS, whose handlers decode JSON themselves
# (see ReadFileTool/WriteFileTool/WebSearchTool/WebFetchTool). The entries here
# are kept as defense-in-depth for if/when those servers are added. The live
# fix for each server-less tool lives in its handler. test_write_file_inline_
# json_args and test_mcp_json_primary_keys_are_all_live pin both halves.
_MCP_JSON_PRIMARY_KEYS: Dict[str, tuple] = {
    "web_search":     ("query", "queries"),
    "web_fetch":      ("url",),
    "read_file":      ("path",),
    "write_file":     ("path",),
    "generate_image": ("prompt",),
}


def _build_mcp_args(tool: str, content: str) -> Dict:
    """Convert fenced-block text content to structured MCP arguments."""
    primaries = _MCP_JSON_PRIMARY_KEYS.get(tool)
    if primaries and content.strip().startswith("{"):
        try:
            decoded = json.loads(content.strip())
        except (json.JSONDecodeError, TypeError):
            decoded = None
        if isinstance(decoded, dict) and any(k in decoded for k in primaries):
            return decoded
    parser = _MCP_ARG_PARSERS.get(tool)
    return parser(content) if parser else {}


async def _call_mcp_tool(
    tool: str,
    content: str,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
    allow_private: bool = False,
) -> Dict:
    """Route a legacy tool call through the MCP manager, with direct fallbacks."""
    mcp = get_mcp_manager()
    if not mcp:
        return await _direct_fallback(tool, content, progress_cb=progress_cb, session_id=session_id, owner=owner, allow_private=allow_private) or {"error": f"MCP manager not available for tool '{tool}'", "exit_code": 1}

    server_id, tool_name = _MCP_TOOL_MAP[tool]
    qualified = f"mcp__{server_id}__{tool_name}"
    args = _build_mcp_args(tool, content)
    result = await mcp.call_tool(qualified, args)

    # If MCP server not connected, try direct fallback
    if isinstance(result, dict) and result.get("exit_code") == 1 and "not connected" in result.get("error", ""):
        fallback = await _direct_fallback(tool, content, progress_cb=progress_cb, session_id=session_id, owner=owner, allow_private=allow_private)
        if fallback:
            return fallback

    # generate_image runs as a text-only MCP tool, so the saved image URL never
    # reaches the agent loop's structured forwarding (which renders the image via
    # buildImageBubble on result["image_url"]). Lift it out of the tool's stdout so
    # the image renders deterministically — no dependence on the model echoing the
    # URL into its prose (which it mangles/hallucinates).
    if tool == "generate_image":
        _promote_image_fields(result)

    return result


def _promote_image_fields(result: Dict) -> None:
    """Lift the image URL (+ prompt/model/size) from a successful generate_image MCP
    text result into structured fields the agent loop already forwards to
    buildImageBubble. Only acts on a dict result with exit_code 0; matches the
    generated-image URL by pattern (absolute or relative) so it's robust to the
    result's wording."""
    if not isinstance(result, dict) or result.get("exit_code") != 0:
        return
    out = result.get("stdout") or ""
    m = re.search(r'(?:https?://[^\s)\]]+)?/api/generated-image/[A-Za-z0-9._-]+', out)
    if not m:
        return
    result["image_url"] = m.group(0).strip()
    for field, pat in (
        ("image_prompt", r'^Generated image for:\s*(.+)$'),
        ("image_model", r'^model:\s*(.+)$'),
        ("image_size", r'^size:\s*(.+)$'),
    ):
        fm = re.search(pat, out, re.M)
        if fm:
            result[field] = fm.group(1).strip()


_BG_MARKERS = {"#!bg", "#bg", "# bg", "#background", "# background", "@background", "# @background"}


def _split_bg_marker(content: str):
    """If the bash content's first non-empty line is a background marker
    (e.g. `#!bg`), return (True, command_without_marker); else (False, content)."""
    lines = content.split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].strip().lower() in _BG_MARKERS:
        del lines[i]
        return True, "\n".join(lines).strip()
    return False, content


def _redact_for_log(text: str) -> str:
    """Strip credential material before a preview reaches the log file.

    The bash preview is the tool's own input, and the agent does put secrets in
    there: the 2026-09-11 logs show `auth="Authorization: Bearer ` cut off by
    the 80-char limit one character before the token. The log viewer redacts on
    read, but the file on disk (and `docker logs`) kept the raw line.
    """
    try:
        from src.agent_logs import redact_line
        return redact_line(text)
    except Exception:
        return text


# Lines that say nothing about what a script does: blank, comments, shebangs,
# and shell option boilerplate (`set -e`, `set -euo pipefail`, `set -o pipefail`).
_PREVIEW_SKIP_RE = re.compile(r"^(?:#.*|set(?:\s+(?:[-+][A-Za-z]+|[A-Za-z]+))+)$")


def _command_preview(content: str, limit: int = 80) -> str:
    """One-line, secret-free summary of a tool's input for the log and the UI.

    The preview used to be the raw first line, so a multi-line script logged as
    `bash: set -e -> exit_code=1` — nothing to diagnose with (three rounds of the
    2026-09-11 logs read exactly that). Skip the boilerplate, show the first
    real command, and say how many more lines follow.
    """
    lines = [line.strip() for line in str(content or "").split("\n")]
    real = [line for line in lines if line and not _PREVIEW_SKIP_RE.match(line)]
    if not real:
        real = [line for line in lines if line]
    if not real:
        return ""
    head = real[0][:limit]
    if len(real) > 1:
        head += f" (+{len(real) - 1} lines)"
    return _redact_for_log(head)


def _failure_detail(result: Any, limit: int = 200) -> str:
    """Why a tool call failed, for the `Tool executed` log line; empty on success.

    `delegate_to_claude_code -> exit_code=1` was the whole record of a failed
    delegation — the reason lived only in the model's context. Surface the
    error (or the tail of the output for a non-zero exit) so the log answers
    it without a replay.
    """
    if not isinstance(result, dict):
        return ""
    code = result.get("exit_code")
    text = result.get("error")
    if text:
        text = str(text)
    elif code not in (None, 0, "0", "n/a"):
        text = str(result.get("stderr") or result.get("output") or "")[-limit:]
    else:
        return ""
    text = " ".join(str(text).split())
    if len(text) > limit:
        text = text[:limit] + "…"
    return _redact_for_log(text) if text else ""


_DELEGATION_INSPECTION_ACTIONS = frozenset({
    "poll", "get", "cancel", "list", "status", "list_repositories", "repositories",
})


def _tool_action(content: Any, default: str = "") -> str:
    """Read an action verb from a structured tool call without failing it."""
    try:
        args = json.loads(content) if isinstance(content, str) else content
    except (TypeError, ValueError):
        return default
    if not isinstance(args, dict):
        return default
    return str(args.get("action") or default).strip().lower()


def _capacity_limited_tool_call(tool: str, content: Any) -> bool:
    """Whether this call may start new worker work.

    ``delegate_*`` multiplexes inspection and lifecycle operations. Blocking a
    poll/list/status behind a full worker limit traps the caller: it cannot
    observe, cancel, or wait for the work occupying the slot.
    """
    if tool in {"delegate_to_agent", "delegate_to_claude_code"}:
        return _tool_action(content, "run") not in _DELEGATION_INSPECTION_ACTIONS
    return tool in {"send_to_session", "pipeline", "create_session"}


def _worker_capacity_result(tool: str, limit: int, active: int) -> Dict[str, Any]:
    available = max(0, limit - active)
    if tool in {"delegate_to_agent", "delegate_to_claude_code"}:
        next_step = (
            f"inspect existing work with {tool} action='list' or action='poll', or cancel it if appropriate."
        )
    else:
        next_step = "Wait for existing work to finish or cancel it before starting another worker."
    return {
        "error": (
            f"Worker capacity reached: {active} active of this chat's limit {limit}. "
            "This is the parent chat's Child workers limit, not the provider-wide concurrent-jobs setting. "
            "Do not retry a start while capacity is unchanged; " + next_step
        ),
        "blocked": True,
        "blocked_reason": "worker_capacity",
        "capacity": {"limit": limit, "active": active, "available": available},
        "capacity_scope": "parent_chat",
        "configuration_hint": "Agents > select the parent chat > Loadout > Child workers. Only the user may raise this ceiling.",
        "exit_code": 1,
    }


async def _direct_fallback(
    tool: str,
    content: str,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
    allow_private: bool = False,
) -> Optional[Dict]:
    _subproc_env = {
        **os.environ,
        "TERM": "xterm-256color",
        "COLUMNS": "120",
        "LINES": "40",
        "HOME": _AGENT_WORKDIR,
    }
    _subproc_env.update(_git_safe_directory_env(_subproc_env))

    try:
        ctx = {
            "progress_cb": progress_cb,
            "subproc_env": _subproc_env,
            "session_id": session_id,
            "owner": owner,
            "allow_private": bool(allow_private),
            # Provider-neutral delegation still needs the concrete tool name
            # for actionable, stable error prefixes.
            "tool_name": tool,
        }

        from src.agent_tools import TOOL_HANDLERS
        if tool in TOOL_HANDLERS:
            return await TOOL_HANDLERS[tool](content, ctx)

    except Exception as e:
        return {"error": f"{tool}: {e}", "exit_code": 1}

    return None


async def _document_tool_dispatch(
    tool: str,
    content: str,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
    allow_private: bool = False,
    document_id: Optional[str] = None,
    document_version: Optional[int] = None,
    document_digest: Optional[str] = None,
) -> Optional[Dict]:
    """Route a document tool through TOOL_HANDLERS with the right ctx shape."""
    from src.agent_tools import TOOL_HANDLERS
    ctx = {
        "session_id": session_id,
        "owner": owner,
        "allow_private": bool(allow_private),
        "tool_name": tool,
        "doc_id": document_id,
        "expected_document_version": document_version,
        "expected_document_digest": document_digest,
    }
    if tool in TOOL_HANDLERS:
        return await TOOL_HANDLERS[tool](content, ctx)
    return None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def _READ_ACTION_TOOLS():
    from src.tool_capabilities import READ_ACTION_TOOLS

    return READ_ACTION_TOOLS


async def execute_tool_block(
    block: Any,
    session_id: Optional[str] = None,
    disabled_tools: Optional[set] = None,
    owner: Optional[str] = None,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    workspace: Optional[str] = None,
    tool_policy: Optional[Any] = None,
    allow_private: bool = False,
    delegation_authorized: Optional[bool] = None,
    tool_discovery: Optional[Any] = None,
    security_context: (
        ToolRunSecurityContext
        | _NoToolSecurityContext
        | _MissingToolSecurityContext
    ) = _MISSING_TOOL_SECURITY_CONTEXT,
    exact_approval: Optional[ExactToolApproval] = None,
) -> Tuple[str, Dict]:
    """Execute a single tool block. Returns (description, result_dict).

    Thin wrapper: bind the per-turn workspace (so the path resolvers + subprocess
    cwd confine to it) for the duration of this call, then delegate. Reset on the
    way out so the binding never leaks to the next tool call.
    """
    if security_context is _MISSING_TOOL_SECURITY_CONTEXT:
        raise TypeError(
            "execute_tool_block requires security_context; pass a "
            "ToolRunSecurityContext or NO_TOOL_SECURITY_CONTEXT explicitly"
        )
    if (
        not isinstance(security_context, ToolRunSecurityContext)
        and security_context is not NO_TOOL_SECURITY_CONTEXT
    ):
        raise TypeError(
            "security_context must be a ToolRunSecurityContext or "
            "NO_TOOL_SECURITY_CONTEXT"
        )

    approval_claimed = False
    if exact_approval is not None:
        # An approval raised by untrusted context must resume in an armed
        # context. One raised by the chat's approval mode had no untrusted
        # context to carry, so only the context itself is required.
        if (
            not isinstance(security_context, ToolRunSecurityContext)
            or (
                exact_approval.pending.external_untrusted_context_seen
                and not security_context.external_untrusted_context_seen
            )
        ):
            return (
                f"{getattr(block, 'tool_type', None)}: BLOCKED",
                {
                    "error": "Exact-action approval requires an armed run security context.",
                    "exit_code": 1,
                    "blocked": True,
                    "policy": "exact_tool_approval",
                },
            )
        if (
            exact_approval.pending.tool_name
            in {"edit_document", "suggest_document", "update_document"}
            and (
                not exact_approval.pending.document_id
                or exact_approval.pending.document_version is None
                or not exact_approval.pending.document_digest
            )
        ):
            return (
                f"{getattr(block, 'tool_type', None)}: BLOCKED",
                {
                    "error": (
                        "The approved document action has no sealed target and "
                        "cannot be executed."
                    ),
                    "exit_code": 1,
                    "blocked": True,
                    "policy": "exact_tool_approval",
                },
            )
        sealed_workspace = exact_approval.pending.workspace
        if sealed_workspace and vet_workspace(sealed_workspace) != sealed_workspace:
            return (
                f"{getattr(block, 'tool_type', None)}: BLOCKED",
                {
                    "error": (
                        "The approved workspace is no longer a valid safe "
                        "directory. Review the action again."
                    ),
                    "exit_code": 1,
                    "blocked": True,
                    "policy": "exact_tool_approval",
                },
            )
        approval_claimed = exact_approval.claim(
            owner=owner,
            session_id=session_id,
            tool_name=getattr(block, "tool_type", None),
            content=getattr(block, "content", None),
            workspace=workspace,
        )
        if not approval_claimed:
            return (
                f"{getattr(block, 'tool_type', None)}: BLOCKED",
                {
                    "error": "The exact-action approval did not match this tool request.",
                    "exit_code": 1,
                    "blocked": True,
                    "policy": "exact_tool_approval",
                },
            )

    if isinstance(security_context, ToolRunSecurityContext) and not approval_claimed:
        decision = security_context.decision_for(
            getattr(block, "tool_type", None),
            getattr(block, "content", None),
        )
        if not decision.allowed:
            logger.warning(
                "External-context policy blocked tool=%r",
                getattr(block, "tool_type", None),
            )
            return blocked_tool_result(
                getattr(block, "tool_type", None),
                decision.reason or "Tool blocked by external-context policy.",
            )

    token = _active_workspace.set(workspace or None)
    try:
        output = await _execute_tool_block_impl(
            block,
            session_id=session_id,
            disabled_tools=disabled_tools,
            owner=owner,
            progress_cb=progress_cb,
            tool_policy=tool_policy,
            allow_private=allow_private,
            delegation_authorized=delegation_authorized,
            tool_discovery=tool_discovery,
            approved_document_id=(
                exact_approval.pending.document_id
                if approval_claimed
                else None
            ),
            approved_document_version=(
                exact_approval.pending.document_version
                if approval_claimed
                else None
            ),
            approved_document_digest=(
                exact_approval.pending.document_digest
                if approval_claimed
                else None
            ),
        )
        if isinstance(security_context, ToolRunSecurityContext):
            security_context.observe_tool_result(
                getattr(block, "tool_type", None),
                output[1],
                getattr(block, "content", None),
            )
        return output
    finally:
        _active_workspace.reset(token)


async def _execute_tool_block_impl(
    block: Any,
    session_id: Optional[str] = None,
    disabled_tools: Optional[set] = None,
    owner: Optional[str] = None,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    tool_policy: Optional[Any] = None,
    allow_private: bool = False,
    delegation_authorized: Optional[bool] = None,
    tool_discovery: Optional[Any] = None,
    approved_document_id: Optional[str] = None,
    approved_document_version: Optional[int] = None,
    approved_document_digest: Optional[str] = None,
) -> Tuple[str, Dict]:
    """Execute a single tool block. Returns (description, result_dict).

    `progress_cb` is forwarded to long-running subprocess tools
    (bash, python) so the agent loop can emit `tool_progress` SSE
    events while the command is in flight. Ignored by other tools.
    """
    from src.tool_implementations import (
        do_search_chats, do_manage_tasks,
        do_manage_skills, do_api_call, do_manage_notes,
        do_manage_calendar, do_manage_wellbeing, is_local_session,
        do_download_model, do_serve_model, do_list_served_models, do_stop_served_model,
        do_tail_serve_output,
        do_list_downloads, do_cancel_download, do_search_hf_models, do_list_cached_models,
        do_list_serve_presets, do_serve_preset, do_adopt_served_model,
        do_list_cookbook_servers,
        do_edit_image, do_trigger_research, do_manage_research, do_resolve_contact,
        do_manage_contact,
        do_vault_search, do_vault_get, do_vault_unlock,
        do_app_api,
    )

    # HACK:
    # This is a temporary workaround for a circular dependency between
    # tool_execution.py and agent_tools.__init__.py.
    #
    # See issue #4277:
    # refactor(tools): Move the registry from __init__.py into a
    # dedicated registry.py module.
    #
    # Do not copy this pattern elsewhere. This import should be removed
    # once the registry refactor is completed.
    try:
        agent_tools_mod = __import__("src.agent_tools", fromlist=["TOOL_HANDLERS"])
        dynamic_handlers = getattr(agent_tools_mod, "TOOL_HANDLERS", {})
    except ImportError:
        dynamic_handlers = {}

    tool = block.tool_type
    content = block.content

    # The block/disable gates below must match every policy-equivalent
    # spelling of the tool name (bare email names alias their mcp__email__
    # form — see email_tool_policy_names), not just the spelling the model
    # happened to emit.
    policy_names = email_tool_policy_names(tool)

    # Global tool toggles can change during an agent turn (including through
    # manage_settings itself), so the turn-start disabled_tools snapshot is not
    # an execution-time authority. Read the small denylist directly, bypassing
    # the ordinary settings TTL cache, and fail closed on malformed/unreadable
    # policy. A missing settings file is the valid fresh-install empty policy.
    try:
        from src.settings import load_disabled_tools_strict
        fresh_global_disabled = set(load_disabled_tools_strict())
    except Exception:
        logger.exception("Tool blocked because global tool policy could not be loaded: tool=%s", tool)
        return f"{tool}: BLOCKED", {
            "error": "The global capability policy could not be loaded. No tool was executed; retry after settings access recovers.",
            "blocked": True,
            "blocked_reason": "global_policy_unavailable",
            "exit_code": 1,
        }
    if not policy_names.isdisjoint(fresh_global_disabled):
        logger.info("Tool blocked by fresh global revocation: tool=%s", tool)
        return f"{tool}: BLOCKED", {
            "error": f"Tool '{tool}' is disabled by the current global settings.",
            "blocked": True,
            "blocked_reason": "fresh_global_disabled",
            "exit_code": 1,
        }

    # Misformatted tool call detection: model put JSON inside ```python``` (or
    # similar) without naming the tool. Common with MiniMax-style outputs.
    # Return a helpful error so the model retries with the correct format.
    if tool in ("python", "json", "xml") and content.strip().startswith("{") and content.strip().endswith("}"):
        try:
            parsed = json.loads(content.strip())
            if isinstance(parsed, dict):
                desc = f"{tool}: misformatted tool call"
                result = {
                    "error": (
                        f"You wrote a JSON object inside a ```{tool}``` block, but that's not a tool call.\n"
                        "To call a tool, use the tool name as the fence tag, e.g.\n"
                        "```resolve_contact\n"
                        "{\"name\": \"...\"}\n"
                        "```\n"
                        "or\n"
                        "```send_email\n"
                        "{\"to\": \"...\", \"subject\": \"...\", \"body\": \"...\"}\n"
                        "```"
                    ),
                    "exit_code": 1,
                }
                return desc, result
        except (ValueError, TypeError):
            pass

    # Reject tools that the user has disabled for this request
    if disabled_tools and not policy_names.isdisjoint(disabled_tools):
        desc = f"{tool}: BLOCKED"
        if tool_requires_private_grant(tool) and allow_private is not True:
            return desc, private_tool_denial(tool)
        result = {"error": f"Tool '{tool}' is disabled by user.", "exit_code": 1}
        logger.info(f"Tool blocked by user: {tool}")
        return desc, result

    # Defense-in-depth for per-agent capability profiles. The prompt/schema
    # layer hides disallowed capabilities; these checks also reject a stale or
    # hand-written tool call so a running agent cannot bypass a profile change.
    _agent_settings = {}
    if session_id:
        try:
            from core.database import get_session_settings
            _agent_settings = get_session_settings(session_id, strict=True) or {}
        except Exception:
            logger.warning("Tool blocked because session policy could not be loaded: session=%s tool=%s", session_id, tool)
            return f"{tool}: BLOCKED", {
                "error": "This chat's capability policy could not be loaded. No tool was executed; retry after settings access recovers.",
                "blocked": True, "blocked_reason": "session_policy_unavailable", "exit_code": 1,
            }
    allow_private = effective_private_grant(
        allow_private, _agent_settings if session_id else None,
    )
    if _agent_settings:
        # Session settings are re-read for every call. A tool disabled after
        # turn preparation must be revoked here before any handler (including
        # turn-local discovery) can run; the incoming disabled_tools snapshot
        # above intentionally cannot see such a mid-turn change.
        _fresh_disabled = {
            str(name) for name in (_agent_settings.get("disabled_tools") or ()) if name
        }
        if not policy_names.isdisjoint(_fresh_disabled):
            logger.info("Tool blocked by fresh session revocation: session=%s tool=%s", session_id, tool)
            return f"{tool}: BLOCKED", {
                "error": f"Tool '{tool}' is disabled by this agent's current settings.",
                "blocked": True,
                "blocked_reason": "fresh_session_disabled",
                "exit_code": 1,
            }
        if _agent_settings.get("workflow_readonly"):
            if tool == "manage_skills":
                try:
                    _skill_action = str(json.loads(content).get("action", "")).lower()
                except (ValueError, TypeError, AttributeError):
                    _skill_action = ""
                if _skill_action not in {"list", "index", "search", "view", "view_ref"}:
                    return f"{tool}: BLOCKED", {"error": "Research workers may load skills, not modify them.", "exit_code": 1}
            elif tool in _READ_ACTION_TOOLS():
                from src.tool_capabilities import action_is_read

                if not action_is_read(tool, content):
                    return f"{tool}: BLOCKED", {
                        "error": f"This worker is read-only: {tool} may only read here (for example "
                                 "list_events). Return the proposed change for the parent to apply.",
                        "blocked": True, "blocked_reason": "workflow_readonly", "exit_code": 1,
                    }
            elif tool.startswith("mcp__"):
                from src.mcp_manager import mcp_call_is_readonly
                _manager = get_mcp_manager()
                _metadata = next((t for t in (_manager.get_all_tools() if _manager else [])
                                  if t.get("qualified_name") == tool), None)
                if not mcp_call_is_readonly(tool, content, _metadata):
                    return f"{tool}: BLOCKED", {
                        "error": "Research workers may call only read-only MCP tools. "
                                 "Return the proposed change for the parent to apply.",
                        "blocked": True, "blocked_reason": "workflow_readonly", "exit_code": 1,
                    }
        # An explicit allowlist remains binding when new MCP tools connect
        # after the profile was saved; a snapshot denylist cannot do that.
        _tool_access = _agent_settings.get("tool_access", "all")
        _enabled = set(_agent_settings.get("enabled_tools") or [])
        _discovery_selected_ok = tool == "discover_tools" and _tool_access == "selected" and bool(_enabled)
        if _tool_access == "none" or (_tool_access == "selected" and policy_names.isdisjoint(_enabled) and not _discovery_selected_ok):
            return f"{tool}: BLOCKED", {
                "error": f"Tool '{tool}' is not in this agent's selected tool bindings.",
                "exit_code": 1,
            }
        _allowed_mcp = _agent_settings.get("allowed_mcp_servers")
        if tool.startswith("mcp__") and isinstance(_allowed_mcp, list) and "*" not in _allowed_mcp:
            _parts = tool.split("__", 2)
            if len(_parts) == 3 and _parts[1] not in set(_allowed_mcp):
                return f"{tool}: BLOCKED", {
                    "error": f"MCP server '{_parts[1]}' is not enabled for this agent.",
                    "exit_code": 1,
                }

        _memory_tool = tool == "manage_memory" or tool == "mcp__memory__manage_memory"
        if _memory_tool:
            _memory_access = str(_agent_settings.get("memory_access") or "write")
            _memory_action = ""
            try:
                _memory_args = json.loads(content) if str(content).lstrip().startswith("{") else None
                if isinstance(_memory_args, dict):
                    _memory_action = str(_memory_args.get("action") or "").lower()
            except Exception:
                pass
            if not _memory_action:
                _memory_action = str(content or "").strip().split("\n", 1)[0].lower()
            if _memory_access == "none" or (_memory_access == "read" and _memory_action not in {"list", "search"}):
                return f"{tool}: BLOCKED", {
                    "error": "This agent has read-only memory access; only list and search are allowed."
                    if _memory_access == "read" else "Memory is disabled for this agent.",
                    "exit_code": 1,
                }

        if tool in {"chat_with_model", "ask_teacher"} and _agent_settings.get("model_access") == "selected":
            _allowed_models = {str(v).casefold() for v in (_agent_settings.get("allowed_models") or [])}
            _requested_model = str(content or "").strip().split("\n", 1)[0]
            try:
                _model_args = json.loads(content) if str(content).lstrip().startswith("{") else None
                if isinstance(_model_args, dict):
                    _requested_model = str(_model_args.get("model") or _model_args.get("name") or _requested_model)
            except Exception:
                pass
            if _requested_model.casefold() not in _allowed_models:
                return f"{tool}: BLOCKED", {
                    "error": f"Model '{_requested_model}' is not in this agent's model allowlist.",
                    "exit_code": 1,
                }

        if tool == "manage_skills" and _agent_settings.get("skill_access") == "selected":
            _allowed_skills = {str(v).casefold() for v in (_agent_settings.get("skill_names") or [])}
            try:
                _skill_args = json.loads(content) if str(content).lstrip().startswith("{") else {}
            except Exception:
                _skill_args = {}
            _requested_skills = []
            if isinstance(_skill_args, dict):
                _requested_skills = _skill_args.get("names") or [_skill_args.get("name")]
            _requested_skills = {str(v).casefold() for v in _requested_skills if v}
            if not _requested_skills or not _requested_skills.issubset(_allowed_skills):
                return f"{tool}: BLOCKED", {
                    "error": "This agent may only load the skills selected in its capability profile.",
                    "exit_code": 1,
                }

    # Capacity is a default policy, not an opt-in profile setting: an empty
    # settings object still means one child at a time. Keep it outside the
    # profile-only guards above, which intentionally do nothing for a plain
    # chat, so that a missing settings row cannot bypass the limit.
    if _capacity_limited_tool_call(tool, content):
        from src.session_settings import effective_worker_limit
        _limit = effective_worker_limit(_agent_settings)
        from src import agent_control as _agent_control
        _live_children = _agent_control.live_children(session_id)
        if _limit <= 0 or _live_children >= _limit:
            logger.info(
                "Tool blocked by worker capacity: tool=%s action=%s active=%s limit=%s",
                tool, _tool_action(content, "run"), _live_children, _limit,
            )
            return f"{tool}: BLOCKED", _worker_capacity_result(tool, _limit, _live_children)

    if tool_policy and any(tool_policy.blocks(name) for name in policy_names):
        desc = f"{tool}: BLOCKED"
        result = {
            "error": f"Execution of tool '{tool}' is forbade by the active guide-only policy.",
            "exit_code": 1,
        }
        logger.warning("Tool policy blocked tool=%s", tool)
        return desc, result

    if tool in _ADMIN_TOOLS and not _owner_is_admin(owner):
        desc = f"{tool}: BLOCKED"
        result = {"error": f"Tool '{tool}' requires an admin user.", "exit_code": 1}
        logger.warning("Admin tool blocked for non-admin owner=%r tool=%s", owner, tool)
        return desc, result

    if is_public_blocked_tool(tool) and not _owner_is_admin(owner):
        desc = f"{tool}: BLOCKED"
        result = {
            "error": (
                f"Tool '{tool}' is restricted to admin users on this deployment. "
                "Ask an admin to perform this action or grant the needed permission."
            ),
            "exit_code": 1,
        }
        logger.warning("Public tool policy blocked owner=%r tool=%s", owner, tool)
        return desc, result

    # Discovery is handled only by the turn-local context supplied by the
    # agent loop.  It runs after every hard/profile/admin gate above and has no
    # fallback handler that could accidentally broaden authority.
    if tool == "discover_tools":
        if tool_discovery is None:
            return "discover_tools: BLOCKED", {
                "error": "Tool discovery is unavailable outside an active agent turn.",
                "blocked": True,
                "exit_code": 1,
            }
        try:
            args = json.loads(content or "{}")
        except (TypeError, ValueError):
            return "discover_tools: invalid arguments", {"error": "Expected JSON arguments.", "exit_code": 1}
        if not isinstance(args, dict) or not isinstance(args.get("query"), str):
            return "discover_tools: invalid arguments", {"error": "A string query is required.", "exit_code": 1}
        query = args["query"].strip()
        max_results = args.get("max_results", 5)
        if not query or len(query) > 500 or isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= 8:
            return "discover_tools: invalid arguments", {
                "error": "query must contain 1-500 characters and max_results must be an integer from 1 to 8.",
                "exit_code": 1,
            }
        fresh_settings = dict(_agent_settings)
        runtime_disabled = set(disabled_tools or ()) | fresh_global_disabled
        if not _owner_is_admin(owner):
            runtime_disabled.update(_ADMIN_TOOLS)
            runtime_disabled.update(name for name in getattr(tool_discovery, "_catalog", {}) if is_public_blocked_tool(name))
        fresh_settings["_runtime_disabled_tools"] = sorted(runtime_disabled)
        fresh_settings["private_vault_access"] = allow_private
        result = await tool_discovery.discover(query, max_results, settings=fresh_settings)
        return f"discover_tools: {query[:80]}", result

    # Shell/Python are unrestricted subprocesses: unlike the dedicated file
    # tools, they can read an absolute path (or walk the vault through a
    # command substitution) before any local sensitivity resolver runs.  The
    # per-chat private-vault grant is therefore a hard execution gate, not a
    # prompt hint.  Keep this before the detached-background branch and before
    # MCP dispatch so neither route can escape the same decision.
    #
    # Without the grant, bash and python may still run inside the bubblewrap
    # sandbox (src/shell_sandbox.py), which sees only the workspace and the
    # read-only system: no /app/data, no vault, no app environment.
    _sandbox_ws = None
    if tool_requires_private_grant(tool) and allow_private is not True:
        _ws = get_active_workspace()
        _sandbox_note = ""
        if tool in ("bash", "python"):
            from src import shell_sandbox

            _why = await asyncio.to_thread(shell_sandbox.unavailable_reason, _ws)
            if not _why:
                _sandbox_ws = os.path.realpath(_ws)
            else:
                _sandbox_note = (" It can run without the grant only in the workspace sandbox, "
                                 f"which is not possible here: {_why}.")
        if _sandbox_ws is None:
            desc = f"{tool}: BLOCKED"
            result = private_tool_denial(tool)
            if _sandbox_note:
                result["error"] += _sandbox_note
            logger.info("Unrestricted tool blocked without private-vault grant: tool=%s session=%r", tool, session_id)
            return desc, result
    _shell_sandbox_workspace.set(_sandbox_ws)


    # Background execution: a `bash` block whose first line is the `#!bg`
    # marker runs DETACHED — returns a job id immediately so the chat stream
    # isn't held open for a multi-minute install/ffmpeg/download. The always-on
    # monitor re-invokes the agent with the full output when the job finishes.
    if tool == "bash" and session_id:
        _is_bg, _bg_cmd = _split_bg_marker(content)
        if _is_bg and _bg_cmd:
            from src import bg_jobs
            rec = bg_jobs.launch(_bg_cmd, session_id=session_id, cwd=agent_cwd(),
                                 sandbox_workspace=_sandbox_ws)
            short = _command_preview(_bg_cmd)
            desc = f"bash (background): {short}"
            result = {
                "output": (
                    f"Started background job `{rec['id']}`. It is running detached; "
                    f"do NOT wait for it or poll it. You will be automatically re-invoked "
                    f"with its full output when it finishes. Continue with other work, or "
                    f"end your turn now and resume when the result arrives. If the user "
                    f"later asks to check progress or stop it, call the manage_bg_jobs "
                    f"tool yourself (output or kill); do not tell them to run a tool "
                    f"command, and do not surface raw tool syntax in your reply."
                ),
                "exit_code": 0,
                "bg_job_id": rec["id"],
            }
            logger.info(f"Tool executed: {desc} -> bg job {rec['id']}")
            return desc, result

    # Route MCP-extracted tools through the MCP manager. Forward
    # the progress callback so long-running subprocess tools
    # (bash, python) can stream `tool_progress` events to the UI.
    # Filesystem reads/writes have a local sensitivity policy and must not be
    # handed to an arbitrary MCP server before that policy runs. The native
    # handlers receive the explicit private-read context below.
    if tool in ("read_file", "write_file"):
        first_line = _command_preview(content)
        desc = f"{tool}: {first_line}"
        result = await _direct_fallback(
            tool,
            content,
            progress_cb=progress_cb,
            session_id=session_id,
            owner=owner,
            allow_private=allow_private,
        ) or {"error": f"{tool}: execution failed", "exit_code": 1}
    elif tool in _MCP_TOOL_MAP:
        first_line = _command_preview(content)
        desc = f"{tool}: {first_line}"
        result = await _call_mcp_tool(
            tool,
            content,
            progress_cb=progress_cb,
            session_id=session_id,
            owner=owner,
            allow_private=allow_private,
        )
    elif tool in ("grep", "glob", "ls", "get_workspace"):
        # Code-navigation tools — no MCP server; run the direct implementation.
        first_line = _command_preview(content)
        desc = f"{tool}: {first_line}"
        result = await _direct_fallback(tool, content, progress_cb=progress_cb, session_id=session_id, owner=owner, allow_private=allow_private) \
            or {"error": f"{tool}: execution failed", "exit_code": 1}
    elif tool in ("apply_patch", "todowrite"):
        first_line = _command_preview(content)
        desc = f"{tool}: {first_line}" if first_line else tool
        result = await _direct_fallback(tool, content, session_id=session_id, owner=owner, allow_private=allow_private) \
            or {"error": f"{tool}: execution failed", "exit_code": 1}
    elif tool == "manage_agent_loadout":
        # Authors/starts worker loadouts; needs session_id to read the calling
        # chat's own policy, which is the ceiling for anything it creates.
        desc = f"manage_agent_loadout: {_command_preview(content, 60)}"
        result = await _direct_fallback(tool, content, session_id=session_id, owner=owner, allow_private=allow_private) \
            or {"error": "manage_agent_loadout: execution failed", "exit_code": 1}
    elif tool == "manage_bg_jobs":
        # Inspect/kill detached `bash` jobs; needs session_id to scope to chat.
        desc = f"manage_bg_jobs: {_command_preview(content)}"
        result = await _direct_fallback(tool, content, session_id=session_id, owner=owner, allow_private=allow_private) \
            or {"error": "manage_bg_jobs: execution failed", "exit_code": 1}
    elif tool in ("create_document", "update_document", "edit_document",
                  "suggest_document", "manage_documents"):
        desc = f"{tool}: {_command_preview(content)}"
        result = await _document_tool_dispatch(
            tool,
            content,
            session_id,
            owner,
            allow_private=allow_private,
            document_id=approved_document_id,
            document_version=approved_document_version,
            document_digest=approved_document_digest,
        ) \
            or {"error": f"{tool}: execution failed", "exit_code": 1}
        if tool in ("edit_document", "suggest_document") and "title" in (result or {}):
            desc = f"{tool}: {result.get('title', '')}"
    elif tool == "search_chats":
        query = content.split("\n")[0].strip()
        desc = f"search_chats: {query[:80]}"
        result = await do_search_chats(query, owner=owner)
    elif tool in ("chat_with_model", "ask_teacher", "list_models"):
        # Migrated to the agent_tools registry (#3629): dispatched through
        # TOOL_HANDLERS with the owner/session ctx these tools need, instead
        # of the legacy dispatch_ai_tool elif. The impls live in
        # src/agent_tools/model_interaction_tools.py.
        first_line = _command_preview(content, 60)
        desc = f"{tool}: {first_line}" if first_line else tool
        result = await _document_tool_dispatch(tool, content, session_id, owner, allow_private) \
            or {"error": f"{tool}: execution failed", "exit_code": 1}
    elif tool in ("create_session", "list_sessions", "send_to_session", "manage_session"):
        # Migrated to the agent_tools registry (#3629): dispatched through
        # TOOL_HANDLERS with the owner/session ctx these tools need. The impls
        # live in src/agent_tools/session_tools.py.
        first_line = _command_preview(content, 60)
        desc = f"{tool}: {first_line}" if first_line else tool
        result = await _document_tool_dispatch(tool, content, session_id, owner, allow_private) \
            or {"error": f"{tool}: execution failed", "exit_code": 1}
    elif tool in ("pipeline", "manage_memory", "ui_control"):
        from src.ai_interaction import dispatch_ai_tool
        desc, result = await dispatch_ai_tool(tool, content, session_id, owner=owner)
    elif tool == "manage_tasks":
        desc = "manage_tasks"
        result = await do_manage_tasks(content, owner=owner)
    elif tool == "manage_skills":
        desc = "manage_skills"
        result = await do_manage_skills(content, owner=owner)
    elif tool == "api_call":
        first_line = _command_preview(content, 60)
        desc = f"api_call: {first_line}"
        result = await do_api_call(content)
    elif tool in ("manage_endpoints", "manage_mcp", "manage_webhooks", "manage_tokens", "manage_settings"):
        # Registry-dispatched (agent_tools.admin_tools); owner threaded for ownership/admin checks.
        desc = tool
        result = await _direct_fallback(tool, content, session_id=session_id, owner=owner, allow_private=allow_private) \
            or {"error": f"{tool}: execution failed", "exit_code": 1}
    elif tool == "manage_notes":
        desc = "manage_notes"
        result = await do_manage_notes(content, owner=owner, allow_private=allow_private)
    elif tool == "manage_wellbeing":
        # Defense in depth. The agent loop already strips this tool from the
        # prompt and schema set for an endpoint scope the owner has denied (see
        # `_apply_private_mcp_filter`), but a model can still emit the fence
        # tag from memory or a stale transcript, so the endpoint is re-checked
        # here and the call is refused rather than answered.
        from src.tools.wellbeing import REMOTE_ENDPOINT_REFUSAL

        desc = "manage_wellbeing"
        if not is_local_session(session_id, owner=owner):
            result = {"error": REMOTE_ENDPOINT_REFUSAL, "exit_code": 1}
            logger.info("manage_wellbeing refused by the owner's Lotus endpoint policy")
        else:
            result = await do_manage_wellbeing(content, owner=owner, session_id=session_id)
    elif tool == "manage_calendar":
        desc = "manage_calendar"
        result = await do_manage_calendar(content, owner=owner)
    elif tool == "download_model":
        desc = "download_model"
        result = await do_download_model(content, owner=owner)
    elif tool == "serve_model":
        desc = "serve_model"
        result = await do_serve_model(content, owner=owner)
    elif tool == "list_served_models":
        desc = "list_served_models"
        result = await do_list_served_models(content, owner=owner)
    elif tool == "stop_served_model":
        desc = "stop_served_model"
        result = await do_stop_served_model(content, owner=owner)
    elif tool == "tail_serve_output":
        desc = "tail_serve_output"
        result = await do_tail_serve_output(content, owner=owner)
    elif tool == "list_downloads":
        desc = "list_downloads"
        result = await do_list_downloads(content, owner=owner)
    elif tool == "cancel_download":
        desc = "cancel_download"
        result = await do_cancel_download(content, owner=owner)
    elif tool == "search_hf_models":
        desc = "search_hf_models"
        result = await do_search_hf_models(content, owner=owner)
    elif tool == "list_cached_models":
        desc = "list_cached_models"
        result = await do_list_cached_models(content, owner=owner)
    elif tool == "app_api":
        desc = "app_api"
        result = await do_app_api(
            content,
            owner=owner,
            allow_private=allow_private,
        )
    elif tool == "list_serve_presets":
        desc = "list_serve_presets"
        result = await do_list_serve_presets(content, owner=owner)
    elif tool == "serve_preset":
        desc = "serve_preset"
        result = await do_serve_preset(content, owner=owner)
    elif tool == "adopt_served_model":
        desc = "adopt_served_model"
        result = await do_adopt_served_model(content, owner=owner)
    elif tool == "list_cookbook_servers":
        desc = "list_cookbook_servers"
        result = await do_list_cookbook_servers(content, owner=owner)
    elif tool == "edit_image":
        desc = "edit_image"
        result = await do_edit_image(content, owner=owner)
    elif tool == "edit_file":
        result = await _direct_fallback(tool, content, session_id=session_id, owner=owner, allow_private=allow_private) or {"error": "edit failed", "exit_code": 1}
        desc = result.get("output") or result.get("error") or "edit_file"
    elif tool == "trigger_research":
        desc = "trigger_research"
        result = await do_trigger_research(content, owner=owner)
    elif tool == "manage_research":
        desc = "manage_research"
        result = await do_manage_research(content, owner=owner)
    elif tool == "resolve_contact":
        desc = "resolve_contact"
        result = await do_resolve_contact(content, owner=owner)
    elif tool == "manage_contact":
        desc = "manage_contact"
        result = await do_manage_contact(content, owner=owner)
    elif tool in ("delegate_to_agent", "delegate_to_claude_code"):
        desc = tool
        result = await _direct_fallback(tool, content, session_id=session_id, owner=owner, allow_private=allow_private) \
            or {"error": f"{tool}: execution failed", "exit_code": 1}
    elif tool == "vault_search":
        desc = "vault_search"
        result = await do_vault_search(content, owner=owner)
    elif tool == "vault_get":
        desc = "vault_get"
        result = await do_vault_get(content, owner=owner)
    elif tool == "vault_unlock":
        desc = "vault_unlock"
        result = await do_vault_unlock(content, owner=owner)
    elif tool in BUILTIN_EMAIL_TOOLS:
        # Bare email tool name from fenced-block models (e.g. Ollama) — route to MCP email server.
        # Non-admin owners never reach here: BUILTIN_EMAIL_TOOLS ⊆ NON_ADMIN_BLOCKED_TOOLS,
        # so is_public_blocked_tool() above already rejected them.
        mcp = get_mcp_manager()
        qualified = f"mcp__email__{tool}"
        desc = f"email: {tool}"
        if mcp:
            _raw = content.strip()
            args = {}
            _args_error = None
            if _raw:
                # A non-empty body is always meant to be the call's arguments,
                # and every email tool takes a JSON object. Anything that
                # isn't one is a correctable error — NOT a silent empty-args
                # call, which would read the DEFAULT mailbox/folder instead of
                # the one the model meant (#3966 class). Only an EMPTY body
                # keeps the no-arg path (e.g. ```list_email_accounts```).
                try:
                    parsed = json.loads(_raw)
                except (json.JSONDecodeError, TypeError) as _je:
                    # Covers both `{account: "work"}` (looks like JSON, bad)
                    # and `account: work` (not JSON at all).
                    _args_error = (
                        f"'{tool}' arguments are not valid JSON ({_je}). "
                        'Send a JSON object, e.g. {"account": "work"} — '
                        "keys and string values need double quotes."
                    )
                else:
                    if isinstance(parsed, dict):
                        args = parsed
                    else:
                        _args_error = (
                            f"'{tool}' arguments must be a JSON object, "
                            'e.g. {"uid": "..."} — got a JSON array/value instead.'
                        )
            if _args_error is not None:
                result = {"error": _args_error, "exit_code": 1}
            else:
                if owner:
                    args = dict(args)
                    args[_EMAIL_MCP_OWNER_ARG] = owner
                result = await mcp.call_tool(qualified, args)
        else:
            result = {"error": "MCP manager not available", "exit_code": 1}
    elif tool.startswith("mcp__"):
        # MCP tool dispatch
        mcp = get_mcp_manager()
        if mcp:
            desc = f"mcp: {tool}"
            args, parse_error = _parse_qualified_mcp_args(tool, content)
            if parse_error:
                result = {"error": parse_error, "exit_code": 1}
            elif tool.startswith("mcp__lotus__") and not is_local_session(
                session_id, owner=owner
            ):
                from src.tools.wellbeing import REMOTE_ENDPOINT_REFUSAL

                result = {"error": REMOTE_ENDPOINT_REFUSAL, "exit_code": 1}
                logger.info("Lotus MCP tool refused by the owner's endpoint policy")
            else:
                if tool.startswith("mcp__email__") and owner:
                    args = dict(args)
                    args[_EMAIL_MCP_OWNER_ARG] = owner
                elif tool.startswith("mcp__lotus__"):
                    args = dict(args)
                    args[_LOTUS_MCP_OWNER_ARG] = owner or "__single_user__"
                result = await mcp.call_tool(tool, args)
        else:
            desc = f"mcp: {tool}"
            result = {"error": "MCP manager not available", "exit_code": 1}


    elif tool == "orchestrate_agents":
        from src.agent_tools.workflow_tools import OrchestrateAgentsTool
        desc = tool
        result = await OrchestrateAgentsTool().execute(content, {
            "session_id": session_id, "owner": owner,
            "allow_private": bool(allow_private),
            "delegation_authorized": delegation_authorized,
        })
    elif tool in dynamic_handlers:
        first_line = _command_preview(content)
        desc = f"registry: {tool} {first_line}".strip()
        res = await _direct_fallback(tool, content, progress_cb=progress_cb, session_id=session_id, owner=owner, allow_private=allow_private)

        if isinstance(res, tuple):
            desc, result = res
        else:
            result = res or {"error": f"{tool}: execution failed", "exit_code": 1}

    else:
        desc = f"unknown: {tool}"
        result = {
            "error": f"Unknown tool: {tool}",
            "exit_code": 1
        }

    _detail = _failure_detail(result)
    logger.info(
        "Tool executed: %s -> exit_code=%s%s",
        desc,
        result.get("exit_code", "n/a") if isinstance(result, dict) else "n/a",
        f" error={_detail}" if _detail else "",
    )
    return desc, result


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------

# Keys handled by the dedicated branches below — never echo them as raw JSON.
_FORMATTER_HANDLED_KEYS = {
    "stdout", "stderr", "exit_code", "content", "size",
    "response", "results", "session_id", "name", "model", "session_name",
    "success", "path", "action", "title", "doc_id", "version", "applied",
    "error", "output", "delegation_state", "delegation_note",
}


def format_tool_result(description: str, result: Dict) -> str:
    """Format a tool result into text for feeding back to the LLM."""
    parts = [f"### {description}"]

    # Provider output is evidence, but it may be an optimistic or stale text
    # payload. Put the normalized local outcome ahead of raw stdout/output so
    # the next agent round cannot mistake "started" prose for a confirmation.
    if result.get("delegation_state"):
        state = str(result["delegation_state"])
        note = str(result.get("delegation_note") or "")
        parts.append(f"**delegation:** `{state}`" + (f" — {note}" if note else ""))

    if "stdout" in result:
        if result["stdout"]:
            parts.append(f"**stdout:**\n```\n{result['stdout']}\n```")
        if result["stderr"]:
            parts.append(f"**stderr:**\n```\n{result['stderr']}\n```")
        parts.append(f"**exit_code:** {result.get('exit_code', 'unknown')}")
    elif "output" in result:
        # bash / python canonical result shape: {"output": ..., "exit_code": ...}
        parts.append(f"```\n{result['output']}\n```")
        if result.get("exit_code") not in (0, None):
            parts.append(f"**exit_code:** {result['exit_code']}")
    elif "content" in result:
        parts.append(f"**content ({result.get('size', '?')} chars):**\n```\n{result['content']}\n```")
    elif "response" in result:
        model = result.get("model", result.get("session_name", ""))
        if model:
            parts.append(f"**{model} responded:**\n{result['response']}")
        else:
            parts.append(result["response"])
    elif "results" in result:
        parts.append(result["results"])
    elif "session_id" in result and "name" in result:
        parts.append(f"Session created: **{result['name']}** (id: `{result['session_id']}`, model: {result.get('model', 'unknown')})")
    elif "success" in result:
        if result["success"]:
            parts.append(f"File written: {result['path']} ({result['size']} bytes)")
        else:
            parts.append(f"Error: {result.get('error', 'unknown')}")
    elif "action" in result:
        action = result["action"]
        if action == "create":
            parts.append(f"Document created: \"{result.get('title', '')}\" (id: {result['doc_id']}, v{result['version']})")
        elif action == "update":
            parts.append(f"Document updated: \"{result.get('title', '')}\" (v{result['version']})")
        elif action == "edit":
            parts.append(f'Document edited: "{result.get("title", "")}" (v{result.get("version", "?")}, {result.get("applied", 0)} edit(s) applied)')
    elif "error" in result:
        parts.append(f"**Error:** {result['error']}")

    # Surface any additional structured payload (events, tasks, notes, calendars,
    # documents, attachments, etc.) that the dedicated branches above don't show.
    # Without this, tools that return {"response": "...", "events": [...]} would
    # silently drop the events list and the model would only see the summary line.
    extra = {k: v for k, v in result.items() if k not in _FORMATTER_HANDLED_KEYS}
    if extra:
        try:
            extra_json = json.dumps(extra, indent=2, default=str, ensure_ascii=False)
            # Cap to avoid blowing the context window on huge payloads.
            if len(extra_json) > 8000:
                extra_json = extra_json[:8000] + f"\n... (truncated, {len(extra_json)} chars total)"
            parts.append(f"**data:**\n```json\n{extra_json}\n```")
        except (TypeError, ValueError):
            pass

    return "\n".join(parts)
