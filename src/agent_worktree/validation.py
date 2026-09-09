"""Validators for everything the agent can influence.

Branch names, commit SHAs, repository slugs and paths all reach git and the
GitHub API from model-controlled input, so each is validated against a strict
allowlist pattern before use. Nothing here builds a shell string: the callers
pass argument arrays, and these functions exist to stop a *valid-looking* value
(`--upload-pack=...`, `..`, `refs/heads/main`) from doing something the operator
did not intend.
"""

from __future__ import annotations

import os
import re
from typing import Optional

# One path segment of a git ref. git's own rules are broader; this is
# deliberately narrower than git-check-ref-format so no ref the agent produces
# can contain a character that is meaningful to a shell, an option parser, or a
# URL.
_REF_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

# owner/name. Both halves must start alphanumeric, which also rejects a leading
# "-" being read as an option by any tool the slug is interpolated into.
_SLUG_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

_SHA_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")

MAX_BRANCH_LEN = 200


def is_valid_sha(value: object) -> bool:
    """True for a full lowercase hex object id (SHA-1 or SHA-256).

    Abbreviated SHAs are rejected on purpose: an approval is bound to an exact
    commit, and a prefix can become ambiguous as the repository grows.
    """
    return isinstance(value, str) and bool(_SHA_RE.match(value))


def is_valid_repo_slug(value: object) -> bool:
    if not isinstance(value, str) or value.count("/") != 1:
        return False
    owner, _, name = value.partition("/")
    if ".." in value or value.endswith(".git"):
        return False
    return bool(_SLUG_PART.match(owner) and _SLUG_PART.match(name))


def normalize_branch(value: object) -> str:
    """Return a safe branch name, or "" when the input is unusable.

    Rejects the shapes that make a branch name dangerous rather than merely
    invalid: leading dashes (option injection), `..` and `@{` (revision syntax),
    `.lock` suffixes and control characters (git refuses them anyway, but the
    check belongs before git sees the value, not after).
    """
    if not isinstance(value, str):
        return ""
    name = value.strip().strip("/")
    if not name or len(name) > MAX_BRANCH_LEN:
        return ""
    if ".." in name or "@{" in name or "\\" in name:
        return ""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        return ""
    segments = name.split("/")
    for seg in segments:
        if not _REF_SEGMENT.match(seg):
            return ""
        if seg.endswith(".lock") or seg.endswith("."):
            return ""
    return name


def validate_agent_branch(value: object, prefix: str) -> str:
    """Return a branch name inside the agent namespace, or "" if it is not.

    The prefix check happens on the *normalized* name so a value like
    `agent/odysseus/../../main` cannot satisfy the prefix and then escape it.
    """
    name = normalize_branch(value)
    if not name or not name.startswith(prefix):
        return ""
    leaf = name[len(prefix):]
    if not leaf or leaf.startswith("/"):
        return ""
    return name


def branch_leaf_from_name(raw: object, prefix: str) -> str:
    """Turn an operator/agent-supplied task name into a namespaced branch.

    Accepts either a bare leaf ("cache-fix") or an already-namespaced branch,
    and returns "" when the result would not be a valid agent branch.
    """
    if not isinstance(raw, str):
        return ""
    candidate = raw.strip()
    if not candidate:
        return ""
    if not candidate.startswith(prefix):
        candidate = prefix + candidate.strip("/")
    return validate_agent_branch(candidate, prefix)


def is_inside(child: str, parent: str) -> bool:
    """True when `child` resolves inside `parent` (symlinks resolved).

    Used to keep every path the worktree operations touch under the configured
    worktree root, so a crafted relative path cannot make git operate on the
    operator's main checkout.
    """
    try:
        c = os.path.normcase(os.path.realpath(child))
        p = os.path.normcase(os.path.realpath(parent))
        if c == p:
            return True
        return os.path.commonpath([c, p]) == p
    except (OSError, ValueError):
        # commonpath raises when the paths sit on different drives/roots, which
        # is exactly the "not inside" case.
        return False


def safe_worktree_path(root: str, branch: str, prefix: str) -> Optional[str]:
    """Directory for a branch's worktree, or None when it would escape `root`.

    The leaf is flattened (slashes become "__") so a nested branch name cannot
    create a directory tree the caller did not expect, and the result is
    re-checked against the root rather than trusted from construction.
    """
    branch = validate_agent_branch(branch, prefix)
    if not branch:
        return None
    leaf = branch[len(prefix):].replace("/", "__")
    if not leaf or leaf in {".", ".."}:
        return None
    candidate = os.path.join(os.path.realpath(root), leaf)
    if not is_inside(candidate, root) or os.path.realpath(candidate) == os.path.realpath(root):
        return None
    return candidate
