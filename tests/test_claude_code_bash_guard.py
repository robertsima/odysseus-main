"""A direct `claude` run from the shell tool is redirected to delegation.

Observed loop (2026-09-10 logs): three rounds of `claude --help | grep ...`
followed by `claude -p "..."` straight from bash, skipping the allowlist,
restricted mode, repo lock and task tracking that delegate_to_claude_code
provides. These tests pin the redirect and the commands that must keep
working.
"""
import asyncio

import pytest

from src.agent_tools import TOOL_HANDLERS
from src.agent_tools import claude_code_guard as guard

pytestmark = pytest.mark.area_security


@pytest.fixture(autouse=True)
def guard_enabled(monkeypatch):
    monkeypatch.delenv(guard.ESCAPE_HATCH_ENV, raising=False)


@pytest.mark.parametrize("command", [
    "claude -p 'fix the bug'",
    "claude --version",
    "claude --help | head -80",
    "claude auth status",
    "/app/data/claude-code/bin/claude -p \"Review the diff\" --output-format json",
    'cd /app/data/development/dog-trainer && /app/data/claude-code/bin/claude -p "Rev"',
    "HOME=/app/data claude -p x",
    "./claude --print hi",
    "make build; claude -p 'do it'",
    "out=$(claude -p 'summarize')",
])
def test_direct_claude_invocations_are_redirected(command):
    result = guard.check(command)
    assert result is not None
    assert result["exit_code"] == 1
    assert result["blocked_reason"] == "claude_code_requires_delegation_tool"
    assert "delegate_to_claude_code" in result["error"]
    assert '"action": "status"' in result["error"]


@pytest.mark.parametrize("command", [
    "grep -rn claude src/",
    "echo claude",
    "python claude_tool.py",
    "ls /app/data/claude-code/bin",
    "cat ~/.claude/settings.json",
    "git log --grep=claude",
    "npm run claude-lint",
    "./claude-helper --go",
    "pytest tests/test_claude_code_tool.py -q",
])
def test_unrelated_commands_still_run(command):
    assert guard.check(command) is None


def test_non_string_input_is_ignored():
    assert guard.check(None) is None
    assert guard.check({"command": "claude -p x"}) is None
    assert guard.check("   ") is None


def test_the_operator_can_turn_the_guard_off(monkeypatch):
    monkeypatch.setenv(guard.ESCAPE_HATCH_ENV, "1")
    assert guard.check("claude -p x") is None


def test_bash_tool_returns_the_redirect_instead_of_running_claude():
    result = asyncio.run(TOOL_HANDLERS["bash"]("claude -p 'hello'", {}))
    assert result["blocked_reason"] == "claude_code_requires_delegation_tool"
    assert "delegate_to_claude_code" in result["error"]


def test_bash_tool_still_runs_ordinary_commands():
    result = asyncio.run(TOOL_HANDLERS["bash"]("echo claude-ok", {}))
    assert result.get("exit_code") == 0
    assert "claude-ok" in (result.get("output") or result.get("stdout") or "")
