"""Redirect a direct `claude` invocation from the shell tool to delegation.

Observed in the 2026-09-10 logs: the primary agent spent three rounds on
``claude --help | grep ...`` and then ran ``claude -p "Rev..."`` straight from
``bash``. That works by accident and skips everything the delegation path
provides — the tool allowlist (no push, no arbitrary shell for the child),
restricted mode, the per-repository lock that keeps two jobs off one working
tree, the background task runner, and the result/git report the harness
records. It also cannot answer permission prompts, so a headless run either
stalls or silently denies.

Like ``agent_worktree.push_guard`` this is narrow: only a command whose
executable basename is exactly ``claude`` is matched, at a command position.
``grep claude file``, ``python claude_tool.py`` and ``echo claude`` are left
alone.
"""

from __future__ import annotations

import os
import re
from typing import Optional

ESCAPE_HATCH_ENV = "ODYSSEUS_AGENT_ALLOW_BASH_CLAUDE"

# `claude`, `/app/data/claude-code/bin/claude`, `./claude`, `"$BIN"/claude`,
# optionally prefixed with env assignments (`HOME=/x claude ...`), at the
# start of a command or after a shell separator. `claude-foo` and `claude.py`
# do not match: the basename must end at whitespace or end of string.
_COMMAND_START = r"(?:^|[;&|(\n])\s*"
_ENV_ASSIGNMENTS = r"(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S*)\s+)*"
_CLAUDE = re.compile(
    _COMMAND_START + _ENV_ASSIGNMENTS + r"(?:\S*/)?claude(?=\s|$)",
    re.IGNORECASE,
)


def _guard_disabled() -> bool:
    return (os.getenv(ESCAPE_HATCH_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def detect_claude_invocation(command: object) -> bool:
    """True when `command` would run the Claude Code binary directly."""
    if not isinstance(command, str) or not command.strip():
        return False
    return _CLAUDE.search(command) is not None


def guidance(command: str) -> str:
    head = command.strip().splitlines()[0][:120]
    return "\n".join([
        "Running Claude Code from bash is not the supported path: it bypasses the "
        "delegation safeguards (tool allowlist, restricted mode, per-repository lock, "
        "task tracking) and a headless run cannot answer permission prompts.",
        "",
        f"  blocked: {head}",
        "",
        "Use the delegate_to_claude_code tool instead:",
        '  1. delegate_to_claude_code {"action": "status"}',
        "     Reports whether Claude Code is installed and signed in, its version, and the "
        "approved repositories. Use this instead of `claude --version`, `--help`, or `auth status`.",
        '  2. delegate_to_claude_code {"repository": "<path from status>", "prompt": "<task>"}',
        "     Waits for the result (action=run, default). Add \"timeout_seconds\" for long jobs, "
        "\"allowed_tools\" to permit a test runner such as \"Bash(pytest:*)\".",
        '  3. delegate_to_claude_code {"action": "start", ...} then {"action": "poll", "task_id": "..."}',
        "     Runs the job in the background so you can keep working or use several repositories.",
        "",
        "The reply carries Claude's result text, permission_denials, and the resulting "
        "branch, commit, and changed files. Publishing still goes through manage_agent_worktree.",
    ])


def check(command: object) -> Optional[dict]:
    """Tool-result dict to return instead of running `command`, or None."""
    if _guard_disabled():
        return None
    if not detect_claude_invocation(command):
        return None
    return {
        "error": guidance(command if isinstance(command, str) else ""),
        "exit_code": 1,
        "blocked_reason": "claude_code_requires_delegation_tool",
    }
