"""Catch git commands that can never succeed from the shell tool.

The publishing credential lives only inside the ``manage_agent_worktree`` flow.
A ``git push`` typed into ``bash`` has no token, no credential helper and no
terminal to prompt at, so it dies with a bare exit 128 and a message about
authentication. The model reads that as "the token is wrong", goes looking for a
GitHub integration that does not exist, and burns rounds getting nowhere. That
exact loop is what this module exists to break.

Detection is deliberately narrow. Only commands that contact a remote are
matched: local work (``status``, ``diff``, ``add``, ``commit``, ``branch``,
``switch``) must keep working normally, because that is how the agent is
supposed to prepare a change.
"""

from __future__ import annotations

import os
import re
from typing import Optional

# `git [-C path] [--git-dir=...] ... push`, anywhere in a compound command.
# Options between `git` and the subcommand are skipped, so `git -C /repo push`
# matches while `git log --grep=push` does not: `--grep=push` is consumed as an
# option, and `log` has already claimed the subcommand slot.
# The leading group anchors `git` to a command position (start of line, or after
# a shell separator) rather than to any whitespace. Without that, `echo git push`
# would be blocked as if it were a real push.
_COMMAND_START = r"(?:^|[;&|(\n])\s*"

_GIT_PUSH = re.compile(
    _COMMAND_START + r"git\s+(?:-[^\s]+(?:\s+[^\s-][^\s]*)?\s+)*push\b",
    re.IGNORECASE,
)

# `gh pr create` and friends, which would need the same missing credential.
_GH_PUBLISH = re.compile(
    _COMMAND_START + r"gh\s+(?:pr\s+create|repo\s+create|release\s+create)\b",
    re.IGNORECASE,
)

ESCAPE_HATCH_ENV = "ODYSSEUS_AGENT_ALLOW_BASH_PUSH"


def _guard_disabled() -> bool:
    """Operators with their own credential setup can turn this off."""
    return (os.getenv(ESCAPE_HATCH_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def detect_publish_command(command: object) -> Optional[str]:
    """Return "git-push" / "gh" for a command that would contact a remote."""
    if not isinstance(command, str) or not command.strip():
        return None
    if _GIT_PUSH.search(command):
        return "git-push"
    if _GH_PUBLISH.search(command):
        return "gh"
    return None


def guidance(kind: str) -> str:
    """The message the agent gets instead of a confusing authentication error."""
    from src.agent_worktree.config import BRANCH_PREFIX, load_config, publish_blockers

    try:
        cfg = load_config()
        blockers = publish_blockers(cfg)
    except Exception:
        cfg, blockers = None, ["configuration could not be read"]

    what = "Pushing to a remote" if kind == "git-push" else "Creating a pull request or release"
    lines = [
        f"{what} from bash is not possible: the shell tool holds no git credential, "
        "so this command can only fail with an authentication error.",
        "",
        "Use the manage_agent_worktree tool instead. It is the only path that has a "
        "credential, and it opens a draft pull request after a human approves:",
        "",
        '  1. manage_agent_worktree {"action": "start", "name": "<short-task-name>"}',
        "     Work inside the worktree path it returns, not in any other checkout.",
        '  2. manage_agent_worktree {"action": "commit", "message": "..."}',
        '  3. manage_agent_worktree {"action": "request_publish", "title": "...", "body": "..."}',
        "  4. Show the operator the changed-file list and the approval command it "
        "returns, then wait for them to give you an approval code.",
        '  5. manage_agent_worktree {"action": "publish", "request_id": "...", '
        '"approval_code": "<code from the operator>"}',
        "",
        f"Branches live under {BRANCH_PREFIX}. Committing locally in bash is fine; "
        "publishing is not.",
    ]
    if blockers:
        lines += [
            "",
            "Publishing is currently unavailable for these reasons, which only the "
            "operator can fix:",
        ]
        lines += [f"  - {reason}" for reason in blockers]
        lines += [
            "",
            "Tell the operator to run `scripts/odysseus-agent-worktree doctor` inside "
            "the container. Do not retry the push and do not go looking for a GitHub "
            "integration or an API token; neither exists here.",
        ]
    return "\n".join(lines)


def check(command: object) -> Optional[dict]:
    """Tool-result dict to return instead of running `command`, or None."""
    if _guard_disabled():
        return None
    kind = detect_publish_command(command)
    if not kind:
        return None
    return {
        "error": guidance(kind),
        "exit_code": 1,
        "blocked_reason": "publish_requires_agent_worktree_tool",
    }
