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

# The fork's approval store (once/always grants handed back to the loop) was
# replaced by upstream's in the 2026-09-18 sync. These tests pin how that
# store behaved and come back with it; see website/upstream-sync-2026-09-18.md.
_FORK_APPROVALS = pytest.mark.skip(
    reason="Re-port backlog: the fork's tool approval store (website/upstream-sync-2026-09-18.md)"
)


# ── approval: the approved call is handed back, verbatim ──────────────────────


@_FORK_APPROVALS
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


@_FORK_APPROVALS
def test_a_denied_or_foreign_approval_is_never_handed_back():
    content = json.dumps({"action": "reset", "repository": "/r",
                          "expected_head": "a" * 40, "expected_target": "b" * 40})
    denied = tool_approvals.request("chat", "manage_git", content, "resets")
    tool_approvals.decide("chat", denied["id"], "deny")
    other = tool_approvals.request("other", "manage_git", content, "resets")
    tool_approvals.decide("other", other["id"], "once")

    assert tool_approvals.approved_unused_calls("chat") == []


@_FORK_APPROVALS
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


def test_an_empty_exception_message_names_where_it_was_raised():
    """A bare `assert` has no message; "AssertionError: no detail" was the
    whole report on 2026-09-18, while the raising frame was the diagnosis."""
    # Raised explicitly: pytest rewrites a literal `assert` to carry a message,
    # which a library's plain assert (the urllib3-future case) does not.
    try:
        raise AssertionError()
    except AssertionError as exc:
        _code, message = repository_sync.describe_remote_failure(exc, operation="fetch")
    assert "no detail" not in message
    assert "raised at" in message and "test_git_tool_failures.py" in message


# ── HTTP: git traffic stays on HTTP/1.1 ──────────────────────────────────────


def test_git_http_is_pinned_to_http1_when_urllib3_future_is_installed():
    """caldav -> niquests -> urllib3-future, which installs AS `urllib3`. Its
    HTTP/2 backend asserted mid-stream on every GitHub clone and fetch; the same
    fetch succeeds 3/3 on HTTP/1.1."""
    try:
        from urllib3 import HttpVersion
    except ImportError:
        pytest.skip("stock urllib3: no HTTP/2 to disable")
    pool = repository_sync._http_pool()
    assert pool.connection_pool_kw.get("disabled_svn") == {HttpVersion.h2, HttpVersion.h3}


def test_the_http1_pin_is_a_no_op_on_stock_urllib3(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_http_version(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "urllib3" and fromlist and "HttpVersion" in fromlist:
            raise ImportError("stock urllib3")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", no_http_version)
    assert repository_sync._http1_only() == {}


# ── merges: status reports a conflict, commit records the merge ──────────────


def _git(cwd, *args):
    import os
    import subprocess

    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)


@pytest.fixture
def conflicted_repo(tmp_path, monkeypatch):
    """A repository left mid-merge on a conflict, as the user's shell merge left one."""
    import shutil

    if not shutil.which("git"):
        pytest.skip("git CLI not available")
    root = tmp_path / "development"
    repo = root / "conflicted"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "dev")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "upstream")
    (repo / "f.txt").write_text("theirs\n")
    (repo / "new.txt").write_text("from upstream\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "theirs")
    theirs = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "dev")
    (repo / "f.txt").write_text("ours\n")
    _git(repo, "commit", "-qam", "ours")
    ours = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "merge", "upstream")
    assert (repo / ".git" / "MERGE_HEAD").exists()
    monkeypatch.setattr(repository_sync, "git_repository_roots", lambda: (root,))
    return SimpleNamespace(path=repo, ours=ours, theirs=theirs)


IDENT = {"author_name": "t", "author_email": "t@x"}


@pytest.mark.asyncio
async def test_status_reports_a_conflicted_merge_instead_of_failing(conflicted_repo):
    """A `ConflictedIndexEntry` has no `mode`; status raised AttributeError and
    reported only "local repository operation failed safely"."""
    status = await repository_local.execute_local("status", str(conflicted_repo.path))
    assert status["merge_in_progress"] is True
    assert status["merge_heads"] == [conflicted_repo.theirs]
    assert status["conflicted"] == ["f.txt"]
    assert "still conflicted" in status["next_step"]


@pytest.mark.asyncio
async def test_commit_is_refused_while_a_conflict_remains(conflicted_repo):
    with pytest.raises(repository_sync.RepositorySyncError) as excinfo:
        await repository_local.execute_local("commit", str(conflicted_repo.path),
                                             message="too early", **IDENT)
    assert excinfo.value.code == "unresolved_conflicts"
    assert "f.txt" in str(excinfo.value)


@pytest.mark.asyncio
async def test_resolving_and_committing_records_a_real_two_parent_merge(conflicted_repo):
    """The commit used to take HEAD as its only parent — a squash git never
    recognises as a merge, so upstream re-conflicts on the next merge."""
    repo = conflicted_repo.path
    (repo / "f.txt").write_text("resolved\n")
    await repository_local.execute_local("stage", str(repo), paths=["f.txt"])
    resolved = await repository_local.execute_local("status", str(repo))
    assert resolved["conflicted"] == [] and resolved["merge_in_progress"] is True

    result = await repository_local.execute_local("commit", str(repo), message="Merge upstream", **IDENT)

    assert result["merged"] == [conflicted_repo.theirs]
    parents = _git(repo, "rev-list", "--parents", "-n1", "HEAD").stdout.split()[1:]
    assert parents == [conflicted_repo.ours, conflicted_repo.theirs]
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert _git(repo, "show", "HEAD:new.txt").stdout == "from upstream\n"
    assert "Already up to date" in _git(repo, "merge", "upstream").stdout


# ── approvals: never ask about a call that cannot run, never re-offer one ─────
#
# The three calls from the 2026-09-18 log, each approved by the user and each
# rejected only when it ran -- then handed back at the start of every turn.

import inspect

import src.agent_loop as agent_loop
from src.agent_tools.git_tools import GitTool

STASH_POP_WITHOUT_PROOF = json.dumps({"action": "stash_pop", "repository": "/r", "index": 0})
PUSH_WITH_FOREIGN_ARG = json.dumps({"action": "push", "repository": "/r", "remote_branch": "dev",
                                    "expected_head": "a" * 40, "expected_target": "b" * 40})
VALID_MERGE = json.dumps({"action": "merge", "repository": "/r", "ref": "upstream/dev",
                          "expected_head": "a" * 40, "expected_target": "b" * 40})


def test_precheck_names_the_missing_revision_proof():
    error = GitTool.precheck(STASH_POP_WITHOUT_PROOF)
    assert error["code"] == "missing_revision"
    assert "expected_target" in error["error"]


def test_precheck_rejects_an_argument_the_action_does_not_take():
    error = GitTool.precheck(PUSH_WITH_FOREIGN_ARG)
    assert error["code"] == "invalid_arguments"
    assert "expected_target" not in error["error"].split("Send only:")[1].split(";")[0]


def test_precheck_rejects_a_push_the_publish_flow_forbids(monkeypatch):
    monkeypatch.setattr("src.agent_worktree.repository_remote.publish_refusal",
                        lambda path, action: "Odysseus repositories must use request_publish/publish")
    push = json.dumps({"action": "push", "repository": "/r", "remote_branch": "dev",
                       "expected_head": "a" * 40})
    error = GitTool.precheck(push)
    assert error["code"] == "use_publish_flow"
    assert "request_publish" in error["error"]


def test_precheck_lets_a_valid_call_through_to_be_held():
    assert GitTool.precheck(VALID_MERGE) is None


@_FORK_APPROVALS
def test_the_loop_prechecks_before_it_holds():
    src = inspect.getsource(agent_loop.stream_agent_loop)
    precheck = src.index("_git_precheck_error(full_command)")
    hold = src.index("_tool_approvals.request(session_id, block.tool_type, full_command")
    assert precheck < hold, "validation must come before the approval card"


@_FORK_APPROVALS
def test_a_retired_approval_is_not_handed_back():
    pending = tool_approvals.request("chat", "manage_git", STASH_POP_WITHOUT_PROOF, "drops")
    tool_approvals.decide("chat", pending["id"], "once")
    assert tool_approvals.approved_unused_calls("chat")          # would be re-offered

    assert tool_approvals.retire_approved_call("chat", "manage_git", STASH_POP_WITHOUT_PROOF)
    assert tool_approvals.approved_unused_calls("chat") == []
    assert not tool_approvals.retire_approved_call("chat", "manage_git", STASH_POP_WITHOUT_PROOF)


@_FORK_APPROVALS
@pytest.mark.asyncio
async def test_running_an_approved_but_invalid_call_retires_its_approval(monkeypatch):
    """The loop the log shows: approved, rejected, re-offered, rejected, ..."""
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda owner: True)
    pending = tool_approvals.request("chat", "manage_git", PUSH_WITH_FOREIGN_ARG, "publishes")
    tool_approvals.decide("chat", pending["id"], "once")

    result = await GitTool().execute(PUSH_WITH_FOREIGN_ARG, {"owner": "admin", "session_id": "chat"})

    assert result["code"] == "invalid_arguments"
    assert tool_approvals.approved_unused_calls("chat") == []
