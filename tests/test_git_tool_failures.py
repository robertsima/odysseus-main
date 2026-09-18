"""The manage_git failures from the 2026-09-18 upstream-sync session.

Each test pins one cause of a call that failed, or failed to explain itself, in
that log: an approval that never stuck, a worktree the git tool could not reach,
a remote-tracking branch that could never be named, and transport errors
reported as "GitHub clone failed" with no cause.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo

from src import tool_approvals
from src.agent_worktree import repository_local, repository_sync


@pytest.fixture(autouse=True)
def reset_approvals():
    tool_approvals._reset_for_tests()
    yield
    tool_approvals._reset_for_tests()


# ── approval: the approved call is handed back, verbatim ──────────────────────


def test_an_approved_call_is_returned_exactly_until_it_runs():
    """"Issue exactly the same call again" failed in practice: the model
    re-inspected state and re-issued the merge with different arguments, a
    different hash, and it was held again. The exact call is on the record."""
    content = json.dumps({"action": "merge", "repository": "/r", "ref": "upstream/dev",
                          "expected_head": "a" * 40, "expected_target": "b" * 40})
    pending = tool_approvals.request("chat", "manage_git", content, "merges")
    assert tool_approvals.approved_unused_calls("chat") == []   # undecided

    tool_approvals.decide("chat", pending["id"], "once")
    calls = tool_approvals.approved_unused_calls("chat")
    assert calls == [{"id": pending["id"], "tool": "manage_git", "command": content}]

    assert tool_approvals.consume_once_grant("chat", "manage_git", content)
    assert tool_approvals.approved_unused_calls("chat") == []   # used up


def test_a_denied_or_foreign_approval_is_never_handed_back():
    content = json.dumps({"action": "reset", "repository": "/r",
                          "expected_head": "a" * 40, "expected_target": "b" * 40})
    denied = tool_approvals.request("chat", "manage_git", content, "resets")
    tool_approvals.decide("chat", denied["id"], "deny")
    other = tool_approvals.request("other", "manage_git", content, "resets")
    tool_approvals.decide("other", other["id"], "once")

    assert tool_approvals.approved_unused_calls("chat") == []


def test_a_truncated_command_is_not_offered_because_it_could_never_match():
    content = json.dumps({"action": "commit", "repository": "/r", "message": "x" * 5000})
    pending = tool_approvals.request("chat", "manage_git", content, "commits")
    tool_approvals.decide("chat", pending["id"], "once")
    assert tool_approvals.approved_unused_calls("chat") == []


# ── roots: the harness can reach the worktrees it creates ─────────────────────


def test_the_worktree_root_stays_reachable_when_claude_code_roots_are_configured(monkeypatch, tmp_path):
    """Saving `claude_code_repository_roots` replaces the default that listed
    /app/data/agent_worktrees, and manage_git refused the worktree that
    manage_agent_worktree had just created, four calls in a row."""
    development, worktrees = tmp_path / "development", tmp_path / "agent_worktrees"
    development.mkdir()
    worktrees.mkdir()
    monkeypatch.setattr(repository_sync, "repository_roots", lambda: (development,))
    monkeypatch.setattr("src.agent_worktree.config.load_config",
                        lambda: SimpleNamespace(worktree_root=str(worktrees)))

    roots = {Path(r).resolve() for r in repository_sync.git_repository_roots()}
    assert roots == {development.resolve(), worktrees.resolve()}


def test_the_worktree_root_is_not_listed_twice(monkeypatch, tmp_path):
    worktrees = tmp_path / "agent_worktrees"
    worktrees.mkdir()
    monkeypatch.setattr(repository_sync, "repository_roots", lambda: (worktrees,))
    monkeypatch.setattr("src.agent_worktree.config.load_config",
                        lambda: SimpleNamespace(worktree_root=str(worktrees)))
    assert len(repository_sync.git_repository_roots()) == 1


def test_an_outside_path_names_the_roots_it_was_checked_against(monkeypatch, tmp_path):
    root = tmp_path / "development"
    root.mkdir()
    monkeypatch.setattr(repository_sync, "git_repository_roots", lambda: (root,))
    with pytest.raises(repository_sync.RepositorySyncError) as excinfo:
        repository_sync._validate_path(str(tmp_path / "elsewhere"))
    assert excinfo.value.code == "outside_roots"
    assert str(root.resolve()) in str(excinfo.value)


# ── refs: remote-tracking branches resolve ────────────────────────────────────


def _repo_with_commit(path: Path):
    repo = Repo.init(str(path))
    blob = Blob.from_string(b"hello\n")
    tree = Tree()
    tree.add(b"README", 0o100644, blob.id)
    commit = Commit()
    commit.tree = tree.id
    commit.author = commit.committer = b"Test <t@example.com>"
    commit.commit_time = commit.author_time = 0
    commit.commit_timezone = commit.author_timezone = 0
    commit.message = b"init"
    for obj in (blob, tree, commit):
        repo.object_store.add_object(obj)
    return repo, commit.id


def test_a_remote_tracking_branch_can_be_named_by_its_short_name(tmp_path):
    """`refs/remotes/` was missing from the candidates, so `upstream/dev` never
    resolved — including two minutes after the user fetched it from a shell."""
    repo, oid = _repo_with_commit(tmp_path)
    repo.refs[b"refs/remotes/upstream/dev"] = oid
    try:
        assert repository_local._resolve_ref(repo, "upstream/dev") == oid
    finally:
        repo.close()


def test_a_local_branch_shadows_a_same_named_remote_one(tmp_path):
    repo, oid = _repo_with_commit(tmp_path)
    repo.refs[b"refs/heads/dev"] = oid
    repo.refs[b"refs/remotes/dev"] = b"f" * 40   # would be wrong if chosen
    try:
        assert repository_local._resolve_ref(repo, "dev") == oid
    finally:
        repo.close()


@pytest.mark.parametrize("ref, fragment", [
    ("upstream/nope", "fetch_branch"),
    ("3b6c169162330cd35c4ce6d14949ef15fd3208a2", "has not been fetched"),
    ("3b6c169", "full 40-character id"),
])
def test_a_miss_says_what_to_do_next(tmp_path, ref, fragment):
    repo, _ = _repo_with_commit(tmp_path)
    try:
        with pytest.raises(repository_sync.RepositorySyncError) as excinfo:
            repository_local._resolve_ref(repo, ref)
    finally:
        repo.close()
    assert excinfo.value.code == "invalid_ref"
    assert fragment in str(excinfo.value)


# ── transport: say why a clone or fetch failed ────────────────────────────────


def test_an_auth_failure_is_named_and_says_retrying_will_not_help():
    from dulwich.client import HTTPUnauthorized

    code, message = repository_sync.describe_remote_failure(
        HTTPUnauthorized("www-authenticate", "https://github.com/o/r"),
        operation="clone", url="https://github.com/o/r")
    assert code == "auth_failed"
    assert "retrying the same call will fail the same way" in message


@pytest.mark.parametrize("exc, code, fragment", [
    (OSError("[Errno -2] Name or service not known"), "network_error", "DNS"),
    (OSError("certificate verify failed"), "network_error", "TLS"),
    (OSError("Max retries exceeded: connection refused"), "network_error", "connection"),
    (ValueError("unexpected response 404 Not Found"), "remote_not_found", "not found"),
])
def test_transport_failures_are_classified(exc, code, fragment):
    got_code, message = repository_sync.describe_remote_failure(exc, operation="fetch")
    assert got_code == code
    assert fragment.lower() in message.lower()


def test_a_token_in_an_exception_message_is_redacted():
    token = "ghp_" + "A" * 36
    code, message = repository_sync.describe_remote_failure(
        RuntimeError(f"failed https://x-access-token:{token}@github.com/o/r token={token}"),
        operation="fetch")
    assert token not in message
    assert "[redacted]" in message
