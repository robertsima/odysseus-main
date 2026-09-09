"""Semi-permanent agent worktree with human-gated publishing.

The agent gets one persistent Git worktree it may edit and test in freely.
Nothing leaves the machine until a human grants a short-lived, single-use
approval bound to the exact repository, branch and commit SHA.

Layout:
  config.py      fail-closed configuration from the environment
  validation.py  ref / repo / path validators (no shell, no globs)
  locking.py     cross-process lock around worktree + approval state
  sensitive.py   classification of changed paths into sensitive categories
  gitcmd.py      argument-array git runner; credentials never hit argv
  approval.py    approval requests, grants, and single-use consumption
  github.py      GitHub App installation tokens and draft PR creation
  service.py     the operations the agent tool and the operator CLI call
"""

from src.agent_worktree.config import (  # noqa: F401
    BRANCH_PREFIX,
    WorktreeConfig,
    load_config,
    publish_blockers,
)

__all__ = [
    "BRANCH_PREFIX",
    "WorktreeConfig",
    "load_config",
    "publish_blockers",
]
