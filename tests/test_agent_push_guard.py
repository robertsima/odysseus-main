"""A push from the shell tool is redirected, not left to fail cryptically.

The observed failure loop: the agent ran `git push` in bash, git exited 128 with
an authentication error because the shell holds no credential, and the model
concluded the GitHub token was wrong and went hunting for an integration that
does not exist. These tests pin the redirect and, just as importantly, pin the
local git commands that must keep working.
"""

import asyncio

import pytest

from src.agent_tools import TOOL_HANDLERS
from src.agent_worktree import push_guard

pytestmark = pytest.mark.area_security


@pytest.fixture(autouse=True)
def guard_enabled(monkeypatch):
    monkeypatch.delenv(push_guard.ESCAPE_HATCH_ENV, raising=False)


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push -u origin agent/odysseus/task",
        "cd /app/data/development/odysseus-main && git push -u origin agent/odysseus/x",
        "git -C /app/repo push",
        "git --git-dir=/app/repo/.git push origin HEAD",
        "gh pr create --draft --fill",
        "gh repo create thing --public",
        "gh release create v1",
        "make build; git push",
        "result=$(git push 2>&1)",
    ],
)
def test_remote_publishing_commands_are_redirected(command):
    assert push_guard.detect_publish_command(command) is not None
    blocked = push_guard.check(command)
    assert blocked is not None
    assert blocked["exit_code"] == 1
    assert "manage_agent_worktree" in blocked["error"]


@pytest.mark.parametrize(
    "command",
    [
        "git status --short",
        "git diff --stat",
        "git add -A",
        'git commit -m "push the button"',
        "git switch -c agent/odysseus/task",
        "git branch -a",
        "git log --oneline --grep=push",
        "git fetch origin",
        "echo git push",
        'echo "run git push later"',
        "python -m pytest",
        "ls -la",
    ],
)
def test_local_and_unrelated_commands_still_run(command):
    assert push_guard.detect_publish_command(command) is None
    assert push_guard.check(command) is None


def test_non_string_input_is_ignored():
    for value in (None, 123, {"command": "git push"}, []):
        assert push_guard.detect_publish_command(value) is None


def test_the_operator_can_turn_the_guard_off(monkeypatch):
    monkeypatch.setenv(push_guard.ESCAPE_HATCH_ENV, "1")
    assert push_guard.check("git push") is None
    # Detection itself is unchanged; only the enforcement is opt-out.
    assert push_guard.detect_publish_command("git push") == "git-push"


def test_the_message_names_the_tool_and_the_steps():
    text = push_guard.guidance("git-push")
    assert "manage_agent_worktree" in text
    for action in ("start", "commit", "request_publish", "publish"):
        assert action in text
    assert "approval_code" in text


def test_the_message_lists_blockers_and_stops_the_hunt(monkeypatch):
    for name in ("ODYSSEUS_AGENT_PUBLISH_ENABLED", "ODYSSEUS_AGENT_REPO"):
        monkeypatch.delenv(name, raising=False)
    text = push_guard.guidance("git-push")
    assert "ODYSSEUS_AGENT_PUBLISH_ENABLED" in text
    assert "doctor" in text
    # The observed dead end was the model looking for a GitHub integration.
    assert "integration" in text


def test_bash_tool_returns_the_redirect_instead_of_running_the_push():
    result = asyncio.run(
        TOOL_HANDLERS["bash"]("cd /tmp && git push -u origin main", {})
    )
    assert result["exit_code"] == 1
    assert "manage_agent_worktree" in result["error"]
    assert result["blocked_reason"] == "publish_requires_agent_worktree_tool"


def test_bash_tool_still_runs_ordinary_commands():
    result = asyncio.run(TOOL_HANDLERS["bash"]("echo hello-from-bash", {}))
    assert result["exit_code"] == 0
    assert "hello-from-bash" in result["output"]
