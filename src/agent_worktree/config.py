"""Fail-closed configuration for the agent worktree.

Every publishing capability is OFF until the operator turns it on explicitly.
A missing, malformed, or unparsable value is treated as "not configured" and
therefore blocks publishing — never as a permissive default. Reading is done
fresh on each call rather than cached at import time so an operator can flip
the flag and restart a worker without a stale module-level snapshot deciding
policy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from src import constants
from src.agent_worktree.validation import (
    is_valid_repo_slug,
    normalize_branch,
)

# The only branch namespace the agent may ever push. Deliberately a module
# constant and not an environment variable: making it configurable would let a
# mis-set env var widen the agent's reach to `main`, and there is no legitimate
# reason for the agent to publish outside its own namespace.
BRANCH_PREFIX = "agent/odysseus/"

# Approval lifetime bounds. A very long TTL turns a one-time approval into a
# standing grant, so the configured value is clamped rather than trusted.
MIN_APPROVAL_TTL_S = 60
MAX_APPROVAL_TTL_S = 3600
DEFAULT_APPROVAL_TTL_S = 900

_TRUE = {"1", "true", "yes", "on"}


def _flag(name: str) -> bool:
    """Strict boolean env read: only explicit affirmatives enable a feature."""
    return (os.getenv(name) or "").strip().lower() in _TRUE


def _text(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _approval_ttl() -> int:
    raw = _text("ODYSSEUS_AGENT_APPROVAL_TTL_SECONDS")
    if not raw:
        return DEFAULT_APPROVAL_TTL_S
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_APPROVAL_TTL_S
    return max(MIN_APPROVAL_TTL_S, min(MAX_APPROVAL_TTL_S, value))


@dataclass(frozen=True)
class WorktreeConfig:
    """Resolved settings. Presence of a field never implies permission."""

    publish_enabled: bool
    repo_slug: str
    source_repo: str
    worktree_root: str
    state_dir: str
    base_branch: str
    approval_ttl_s: int
    api_base: str
    app_id: str
    installation_id: str
    private_key_path: str
    fallback_token_env: str
    _fallback_token_present: bool = field(default=False, repr=False)

    @property
    def remote_url(self) -> str:
        """HTTPS remote derived from the allowlisted slug.

        Derived rather than configured so the push target cannot be pointed at
        an arbitrary host by a second environment variable.
        """
        return f"https://github.com/{self.repo_slug}.git" if self.repo_slug else ""

    @property
    def has_github_app(self) -> bool:
        return bool(self.app_id and self.installation_id and self.private_key_path)

    @property
    def has_any_credential(self) -> bool:
        return self.has_github_app or self._fallback_token_present


def load_config() -> WorktreeConfig:
    repo_slug = _text("ODYSSEUS_AGENT_REPO")
    if not is_valid_repo_slug(repo_slug):
        repo_slug = ""

    source_repo = _text("ODYSSEUS_AGENT_SOURCE_REPO")
    if not source_repo:
        from src.runtime_paths import get_app_root

        source_repo = get_app_root()
    source_repo = os.path.realpath(os.path.expanduser(source_repo))

    # constants.DATA_DIR is read through the module rather than bound at import
    # time so an operator (or a test) can relocate the data directory without a
    # stale snapshot deciding where approval state lives.
    worktree_root = _text("ODYSSEUS_AGENT_WORKTREE_ROOT") or os.path.join(
        constants.DATA_DIR, "agent_worktrees"
    )
    worktree_root = os.path.realpath(os.path.expanduser(worktree_root))

    state_dir = _text("ODYSSEUS_AGENT_STATE_DIR") or os.path.join(
        constants.DATA_DIR, "agent_worktree"
    )
    state_dir = os.path.realpath(os.path.expanduser(state_dir))

    base_branch = normalize_branch(_text("ODYSSEUS_AGENT_BASE_BRANCH")) or "dev"

    fallback_env = "ODYSSEUS_AGENT_GITHUB_TOKEN"

    return WorktreeConfig(
        publish_enabled=_flag("ODYSSEUS_AGENT_PUBLISH_ENABLED"),
        repo_slug=repo_slug,
        source_repo=source_repo,
        worktree_root=worktree_root,
        state_dir=state_dir,
        base_branch=base_branch,
        approval_ttl_s=_approval_ttl(),
        api_base=(_text("ODYSSEUS_GITHUB_API_BASE") or "https://api.github.com").rstrip("/"),
        app_id=_text("ODYSSEUS_GITHUB_APP_ID"),
        installation_id=_text("ODYSSEUS_GITHUB_APP_INSTALLATION_ID"),
        private_key_path=_text("ODYSSEUS_GITHUB_APP_PRIVATE_KEY_PATH"),
        fallback_token_env=fallback_env,
        _fallback_token_present=bool(_text(fallback_env)),
    )


def publish_blockers(cfg: Optional[WorktreeConfig] = None) -> List[str]:
    """Reasons publishing must not proceed. Empty list means it may.

    Callers treat a non-empty list as a hard stop. Anything that cannot be
    positively verified is reported as a blocker, so a partially configured
    deployment fails closed instead of attempting a push with half a config.
    """
    cfg = cfg or load_config()
    blockers: List[str] = []
    if not cfg.publish_enabled:
        blockers.append(
            "publishing is disabled (set ODYSSEUS_AGENT_PUBLISH_ENABLED=1 to enable)"
        )
    if not cfg.repo_slug:
        blockers.append(
            "ODYSSEUS_AGENT_REPO is unset or not a valid owner/name slug"
        )
    if not cfg.has_any_credential:
        blockers.append(
            "no GitHub credential configured (GitHub App triple, or "
            f"{cfg.fallback_token_env} as a fallback)"
        )
    elif cfg.has_github_app and not os.path.isfile(cfg.private_key_path):
        blockers.append("ODYSSEUS_GITHUB_APP_PRIVATE_KEY_PATH does not point at a file")
    if not os.path.exists(os.path.join(cfg.source_repo, ".git")):
        blockers.append(f"source repository {cfg.source_repo} is not a git checkout")
    return blockers
