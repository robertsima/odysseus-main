"""A push from the shell tool is redirected, not left to fail cryptically.

The observed failure loop: the agent ran `git push` in bash, git exited 128 with
an authentication error because the shell holds no credential, and the model
concluded the GitHub token was wrong and went hunting for an integration that
does not exist. These tests pin the redirect and, just as importantly, pin the
local git commands that must keep working.

Second observed failure (2026-09-10): a push in a THIRD-PARTY checkout
(`/app/data/development/dog-trainer`, remote `github.com/robertsima/Umni`) was
redirected to `manage_agent_worktree`, which publishes only the Odysseus
repository. The agent created an Odysseus branch `agent/odysseus/umni-publish`,
removed it, hunted for a GitHub integration and reported success without
pushing anything. The guard is now repo-aware.
"""

import asyncio

import pytest

from src.agent_tools import TOOL_HANDLERS
from src.agent_worktree import push_guard

pytestmark = pytest.mark.area_security


@pytest.fixture(autouse=True)
def guard_enabled(monkeypatch, tmp_path):
    monkeypatch.delenv(push_guard.ESCAPE_HATCH_ENV, raising=False)
    # A global ~/.git-credentials would make any repository look credentialed,
    # so pin HOME at an empty directory for deterministic results.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)


@pytest.fixture
def odysseus_repo(tmp_path, monkeypatch):
    """Point the worktree config at a throwaway checkout so "is this the
    Odysseus repo" is decided by configuration, not by the test's cwd."""
    repo = tmp_path / "odysseus-main"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("ODYSSEUS_AGENT_SOURCE_REPO", str(repo))
    monkeypatch.setenv("ODYSSEUS_AGENT_WORKTREE_ROOT", str(tmp_path / "agent_worktrees"))
    return repo


@pytest.fixture
def foreign_repo(tmp_path):
    """A checkout with an https remote and no credential of any kind."""
    repo = tmp_path / "dog-trainer"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/robertsima/Umni.git\n',
        encoding="utf-8",
    )
    return repo


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
        # The agent improvising a credential inline, seen 2026-09-11 round 5.
        "git -c credential.helper='!f() { echo username=x; echo password=y; }; f' push origin main",
        'git -c "credential.helper=store --file=/tmp/c" push',
    ],
)
def test_remote_publishing_commands_are_redirected(command, odysseus_repo):
    assert push_guard.detect_publish_command(command) is not None
    blocked = push_guard.check(command, cwd=str(odysseus_repo))
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


def test_a_push_in_a_third_party_repo_is_not_sent_to_the_worktree_tool(odysseus_repo, foreign_repo):
    """manage_agent_worktree publishes ONE repository. Naming it here is what
    produced the agent/odysseus/umni-publish branch for someone else's project."""
    blocked = push_guard.check("git push origin main", cwd=str(foreign_repo))
    assert blocked is not None
    assert blocked["blocked_reason"] == "publish_needs_repository_credential"
    assert blocked["repository"] == str(foreign_repo)
    text = blocked["error"]
    assert "cannot publish it" in text
    assert "Do not start an Odysseus worktree" in text
    assert "github.com/robertsima/Umni.git" in text
    # Committing locally is still the way forward, and the hunt is closed off.
    assert "commit" in text
    assert "integration" in text


def test_the_target_repository_comes_from_the_command_then_the_cwd(odysseus_repo, foreign_repo):
    assert push_guard.target_repository(f"cd {foreign_repo} && git push", cwd=str(odysseus_repo)) == str(foreign_repo)
    assert push_guard.target_repository(f"git -C {foreign_repo} push", cwd=str(odysseus_repo)) == str(foreign_repo)
    assert push_guard.target_repository(f"git --git-dir={foreign_repo}/.git push", cwd=str(odysseus_repo)) == str(foreign_repo)
    assert push_guard.target_repository("git push", cwd=str(odysseus_repo)) == str(odysseus_repo)


def test_an_agent_odysseus_branch_is_odysseus_work_wherever_it_is_typed(odysseus_repo, foreign_repo):
    blocked = push_guard.check("git push -u origin agent/odysseus/task", cwd=str(foreign_repo))
    assert blocked["blocked_reason"] == "publish_requires_agent_worktree_tool"


@pytest.mark.parametrize("config_line", [
    '[remote "origin"]\n\turl = https://token:x@github.com/robertsima/Umni.git\n',
    '[remote "origin"]\n\turl = git@github.com:robertsima/Umni.git\n',
    '[credential]\n\thelper = manager\n[remote "origin"]\n\turl = https://github.com/robertsima/Umni.git\n',
    '[credential]\n\thelper = !gh auth git-credential\n[remote "origin"]\n\turl = https://github.com/robertsima/Umni.git\n',
])
def test_a_third_party_repo_with_its_own_credential_is_left_alone(tmp_path, odysseus_repo, config_line):
    """Blocking a push that would have succeeded is the regression the user
    reported as "git integration stopped working"."""
    repo = tmp_path / "credentialed"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(config_line, encoding="utf-8")
    assert push_guard.repository_has_own_credential(str(repo)) is True
    assert push_guard.check("git push origin main", cwd=str(repo)) is None


_UMNI = '[remote "origin"]\n\turl = https://github.com/robertsima/Umni.git\n'


def _repo_with_config(tmp_path, name, config):
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(config, encoding="utf-8")
    return repo


def test_a_store_helper_with_a_populated_store_is_left_alone(tmp_path, odysseus_repo):
    store = tmp_path / "creds"
    store.write_text("https://x:y@github.com\n", encoding="utf-8")
    repo = _repo_with_config(tmp_path, "stored", f"[credential]\n\thelper = store --file={store}\n" + _UMNI)
    assert push_guard.repository_has_own_credential(str(repo)) is True
    assert push_guard.check("git push origin main", cwd=str(repo)) is None


def test_a_store_helper_with_an_empty_store_is_blocked_and_named(tmp_path, odysseus_repo):
    """2026-09-11: the dog-trainer checkout survived a container rebuild on
    the data volume, its config still said `helper = store`, and the home
    directory the store lived in did not. The guard waved the push through
    as credentialed and git exited 128."""
    repo = _repo_with_config(tmp_path, "rebuilt", "[credential]\n\thelper = store\n" + _UMNI)
    assert push_guard.repository_has_own_credential(str(repo)) is False
    blocked = push_guard.check("git push origin main", cwd=str(repo))
    assert blocked["blocked_reason"] == "publish_needs_repository_credential"
    expected_store = push_guard._global_credential_stores()[0]
    assert push_guard.empty_credential_store(str(repo)) == expected_store
    assert expected_store in blocked["error"]
    assert "rebuilt" in blocked["error"]
    assert "refill" in blocked["error"]


def test_a_cache_helper_is_not_a_credential_after_a_restart(tmp_path, odysseus_repo):
    repo = _repo_with_config(tmp_path, "cached", "[credential]\n\thelper = cache --timeout=3600\n" + _UMNI)
    assert push_guard.repository_has_own_credential(str(repo)) is False
    assert push_guard.check("git push origin main", cwd=str(repo)) is not None


def test_a_global_credential_store_also_releases_the_guard(tmp_path, monkeypatch, odysseus_repo, foreign_repo):
    home = tmp_path / "home"
    (home / ".git-credentials").write_text("https://x:y@github.com\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    assert push_guard.check("git push origin main", cwd=str(foreign_repo)) is None
    # ...but Odysseus itself still goes through the approval flow.
    blocked = push_guard.check("git push origin main", cwd=str(odysseus_repo))
    assert blocked["blocked_reason"] == "publish_requires_agent_worktree_tool"


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


def test_bash_tool_returns_the_redirect_instead_of_running_the_push(odysseus_repo):
    result = asyncio.run(
        TOOL_HANDLERS["bash"](f"cd {odysseus_repo} && git push -u origin main", {})
    )
    assert result["exit_code"] == 1
    assert "manage_agent_worktree" in result["error"]
    assert result["blocked_reason"] == "publish_requires_agent_worktree_tool"


def test_bash_tool_blocks_a_credential_less_third_party_push_with_its_own_guidance(odysseus_repo, foreign_repo):
    result = asyncio.run(
        TOOL_HANDLERS["bash"](f"cd {foreign_repo} && git push origin main", {})
    )
    assert result["exit_code"] == 1
    assert result["blocked_reason"] == "publish_needs_repository_credential"


def test_bash_tool_still_runs_ordinary_commands():
    result = asyncio.run(TOOL_HANDLERS["bash"]("echo hello-from-bash", {}))
    assert result["exit_code"] == 0
    assert "hello-from-bash" in result["output"]
