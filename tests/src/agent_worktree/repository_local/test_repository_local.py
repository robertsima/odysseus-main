import json
import os
from types import SimpleNamespace

import pytest
from dulwich import porcelain
from dulwich.index import IndexEntry
from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo

from src.agent_tools.git_tools import GitTool
from src.agent_worktree import repository_local as local
from src.agent_worktree import repository_sync as sync

def test_status_rejects_ntfs_alternate_stream_before_content_helpers(monkeypatch):
    # Model a dulwich validator that permits colons, as in the failing CI run.
    monkeypatch.setattr(sync, "validate_path_element_ntfs", lambda _part: True)

    class Index:
        def is_sparse(self):
            return False

        def __len__(self):
            return 1

        def __iter__(self):
            return iter([b"file:stream"])

    repo = SimpleNamespace(
        path="/approved/repo",
        get_config=lambda: SimpleNamespace(),
        open_index=lambda config=None: Index(),
    )
    monkeypatch.setattr(
        sync, "get_tree_changes", lambda *a, **k: pytest.fail("content helper ran")
    )
    with pytest.raises(sync.RepositorySyncError) as exc:
        sync._status(repo)
    assert exc.value.code == "unsafe_tree_path"


def test_tree_rejects_ntfs_alternate_stream_path(repository, monkeypatch):
    # The explicit check must not depend on dulwich's version-specific rules.
    monkeypatch.setattr(sync, "validate_path_element_ntfs", lambda _part: True)
    with Repo(str(repository)) as repo:
        blob = Blob.from_string(b"unsafe")
        repo.object_store.add_object(blob)
        tree = Tree()
        tree.add(b"file:stream", 0o100644, blob.id)
        repo.object_store.add_object(tree)
        commit = Commit()
        commit.tree = tree.id
        commit.parents = [repo.refs[b"HEAD"]]
        commit.author = commit.committer = b"Test <test@example.com>"
        commit.author_time = commit.commit_time = 1
        commit.author_timezone = commit.commit_timezone = 0
        commit.message = b"unsafe tree fixture"
        repo.object_store.add_object(commit)

        with pytest.raises(sync.RepositorySyncError) as exc:
            sync._validate_tree(repo, commit.id)
    assert exc.value.code == "unsafe_tree_path"

_REPORT_BACKLOG = pytest.mark.skip(
    reason="Re-port backlog: uses fork-only internals replaced by upstream's agent core (website/upstream-sync-2026-09-18.md)"
)

_NEUTRAL_GIT_FIELDS = {
    "repository": "",
    "paths": [],
    "name": "",
    "ref": "",
    "message": "",
    "author_name": "",
    "author_email": "",
    "limit": 0,
    "staged": False,
    "remote_branch": "",
    "remote": "",
    "expected_head": "",
    "expected_target": "",
}


def _expanded_git_args(action, repository, **overrides):
    return {
        "action": action,
        **_NEUTRAL_GIT_FIELDS,
        "repository": str(repository),
        **overrides,
    }


def _allow_git_tool(monkeypatch):
    monkeypatch.setattr(
        "src.tool_security.owner_is_admin_or_single_user", lambda _owner: True
    )


@pytest.fixture
def repository(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    path = root / "project"
    path.mkdir()
    repo = porcelain.init(str(path))
    # Bytes, not write_text: on Windows write_text stores CRLF, but the commit
    # holds LF (core.autocrlf=true on the runner), and the diff hashes raw
    # worktree bytes, so README.md would read as changed.
    (path / "README.md").write_bytes(b"one\n")
    porcelain.add(repo, ["README.md"])
    porcelain.commit(
        repo,
        message=b"initial",
        author=b"Test <test@example.com>",
        committer=b"Test <test@example.com>",
    )
    config = repo.get_config()
    config.set((b"user",), b"name", b"Local User")
    config.set((b"user",), b"email", b"local@example.com")
    config.set((b"remote", b"origin"), b"url", b"https://github.com/acme/example.git")
    config.write_to_path()
    repo.close()
    monkeypatch.setattr(sync, "repository_roots", lambda: (root,))
    monkeypatch.setattr(sync, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(sync, "PERSONAL_DIR", str(tmp_path / "data" / "personal_docs"))
    return path


@pytest.mark.asyncio
async def test_status_log_branches_and_remotes_do_not_require_upstream(repository):
    status = await local.execute_local("status", str(repository))
    assert status["status"]["clean"] is True
    assert len(status["head"]) == 40 and status["branch"] in {"main", "master"}
    log = await local.execute_local("log", str(repository), limit=1)
    assert len(log["commits"]) == 1 and log["commits"][0]["message"] == "initial"
    branches = await local.execute_local("branches", str(repository))
    assert branches["current"] in {"main", "master"}
    assert len(branches["head"]) == 40
    assert branches["branches"] == [
        {"name": branches["current"], "target": branches["head"]}
    ]
    remotes = await local.execute_local("remotes", str(repository))
    assert remotes["remotes"] == [
        {"name": "origin", "url": "https://github.com/acme/example.git"}
    ]


@pytest.mark.asyncio
async def test_expanded_git_log_uses_bounded_default_limit(
    repository, monkeypatch
):
    _allow_git_tool(monkeypatch)
    with Repo(str(repository)) as repo:
        for index in range(24):
            (repository / "README.md").write_text(
                f"revision {index}\n", encoding="utf-8"
            )
            porcelain.add(repo, ["README.md"])
            porcelain.commit(
                repo,
                message=f"revision {index}".encode(),
                author=b"Test <test@example.com>",
                committer=b"Test <test@example.com>",
            )

    result = await GitTool().execute(
        json.dumps(_expanded_git_args("log", repository)),
        {"owner": "admin"},
    )

    assert result["exit_code"] == 0
    assert len(result["result"]["commits"]) == 20
    assert result["result"]["commits"][0]["message"] == "revision 23"


@pytest.mark.asyncio
async def test_stage_diff_commit_unstage_round_trip(repository):
    readme = repository / "README.md"
    readme.write_text("two\n", encoding="utf-8")
    unstaged = await local.execute_local("diff", str(repository), staged=False)
    assert "-one" in unstaged["diff"] and "+two" in unstaged["diff"]
    await local.execute_local("stage", str(repository), paths=["README.md"])
    staged = await local.execute_local("diff", str(repository), staged=True)
    assert staged["changed_files"] == 1
    await local.execute_local("unstage", str(repository), paths=["README.md"])
    assert (await local.execute_local("diff", str(repository), staged=True))[
        "changed_files"
    ] == 0
    await local.execute_local("stage", str(repository), paths=["README.md"])
    committed = await local.execute_local(
        "commit", str(repository), message="update readme"
    )
    assert len(committed["commit"]) == 40
    assert (await local.execute_local("status", str(repository)))["status"][
        "clean"
    ] is True


@pytest.mark.asyncio
async def test_expanded_git_commit_uses_configured_identity(repository, monkeypatch):
    _allow_git_tool(monkeypatch)
    (repository / "README.md").write_text("expanded commit\n", encoding="utf-8")
    await local.execute_local("stage", str(repository), paths=["README.md"])

    result = await GitTool().execute(
        json.dumps(
            _expanded_git_args(
                "commit", repository, message="use configured identity"
            )
        ),
        {"owner": "admin"},
    )

    assert result["exit_code"] == 0
    with Repo(str(repository)) as repo:
        commit = repo[result["result"]["commit"].encode()]
        assert commit.author == b"Local User <local@example.com>"


@pytest.mark.asyncio
async def test_new_file_stage_and_explicit_commit_identity(repository):
    (repository / "new.txt").write_text("new\n", encoding="utf-8")
    await local.execute_local("stage", str(repository), paths=["new.txt"])
    result = await local.execute_local(
        "commit",
        str(repository),
        message="add file",
        author_name="Explicit",
        author_email="explicit@example.com",
    )
    with Repo(str(repository)) as repo:
        assert (
            repo[result["commit"].encode()].author == b"Explicit <explicit@example.com>"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected_code"),
    [
        ("stage", "invalid_paths"),
        ("unstage", "invalid_paths"),
        ("commit", "invalid_message"),
        ("branch", "invalid_ref"),
        ("tag", "invalid_ref"),
        ("switch", "invalid_branch"),
        ("set_upstream", "invalid_branch"),
    ],
)
async def test_expanded_git_call_preserves_required_empty_values_for_validation(
    repository, monkeypatch, action, expected_code
):
    _allow_git_tool(monkeypatch)

    result = await GitTool().execute(
        json.dumps(_expanded_git_args(action, repository)),
        {"owner": "admin"},
    )

    assert result["exit_code"] == 1
    assert result["code"] == expected_code


@pytest.mark.asyncio
@pytest.mark.parametrize("action, expected_code", [
    ("push", "missing_revision"),
    ("merge", "missing_revision"),
    # Deleting a branch is refused by policy before any revision proof is
    # read (tests/test_git_delete_secret_policy.py).
    ("delete_branch", "forbidden_by_policy"),
])
async def test_expanded_risky_git_call_refuses_empty_revision_proofs(
    repository, monkeypatch, action, expected_code
):
    _allow_git_tool(monkeypatch)

    result = await GitTool().execute(
        json.dumps(_expanded_git_args(action, repository)),
        {"owner": "admin", "session_id": "empty-revision-proof"},
    )

    assert result["exit_code"] == 1
    assert result["code"] == expected_code


@pytest.mark.asyncio
async def test_branch_and_tag_create_without_overwrite(repository):
    branch = await local.execute_local("branch", str(repository), name="feature/test")
    tag = await local.execute_local("tag", str(repository), name="v1-test")
    assert branch["branch"] == "feature/test" and tag["tag"] == "v1-test"
    for action, name in (("branch", "feature/test"), ("tag", "v1-test")):
        with pytest.raises(sync.RepositorySyncError) as exc:
            await local.execute_local(action, str(repository), name=name)
        assert exc.value.code == "ref_exists"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "paths",
    [
        ["."],
        ["*.py"],
        ["../outside"],
        [".git/config"],
        [],
        ["C:/outside.txt"],
        [r"C:\outside.txt"],
        [" README.md"],
        ["README.md "],
    ],
)
async def test_stage_rejects_broad_or_unsafe_paths(repository, paths):
    with pytest.raises(sync.RepositorySyncError) as exc:
        await local.execute_local("stage", str(repository), paths=paths)
    assert exc.value.code == "invalid_paths"


@pytest.mark.asyncio
async def test_stage_rejects_sensitive_files_and_links(repository, tmp_path):
    (repository / ".env").write_text("SECRET=x\n", encoding="utf-8")
    with pytest.raises(sync.RepositorySyncError) as exc:
        await local.execute_local("stage", str(repository), paths=[".env"])
    assert exc.value.code == "sensitive_path"
    if os.name != "nt":
        outside = tmp_path / "outside"
        outside.write_text("secret", encoding="utf-8")
        (repository / "linked.txt").symlink_to(outside)
        with pytest.raises(sync.RepositorySyncError) as exc:
            await local.execute_local("stage", str(repository), paths=["linked.txt"])
        assert exc.value.code == "unsafe_worktree"


@pytest.mark.asyncio
async def test_stage_rejects_hardlinked_file_and_linked_parent(repository, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    linked = repository / "hardlink.txt"
    os.link(outside, linked)
    with pytest.raises(sync.RepositorySyncError) as exc:
        await local.execute_local("stage", str(repository), paths=["hardlink.txt"])
    assert exc.value.code == "unsafe_worktree"
    if os.name != "nt":
        parent = repository / "linked-parent"
        parent.symlink_to(tmp_path, target_is_directory=True)
        with pytest.raises(sync.RepositorySyncError) as exc:
            await local.execute_local(
                "stage", str(repository), paths=["linked-parent/new.txt"]
            )
        assert exc.value.code == "unsafe_worktree"


@pytest.mark.asyncio
async def test_diff_rejects_sensitive_file_already_committed_in_head(repository):
    (repository / ".env").write_text("SECRET=x\n", encoding="utf-8")
    with Repo(str(repository)) as repo:
        porcelain.add(repo, [".env"])
        porcelain.commit(
            repo,
            message=b"legacy secret",
            author=b"Test <test@example.com>",
            committer=b"Test <test@example.com>",
        )
    with pytest.raises(sync.RepositorySyncError) as exc:
        await local.execute_local("diff", str(repository))
    assert exc.value.code == "sensitive_path"


@pytest.mark.asyncio
async def test_malicious_head_tree_path_is_rejected_before_diff(repository):
    with Repo(str(repository)) as repo:
        blob = Blob.from_string(b"unsafe")
        repo.object_store.add_object(blob)
        metadata = Tree()
        metadata.add(b"config", 0o100644, blob.id)
        repo.object_store.add_object(metadata)
        root = Tree()
        root.add(b".git", 0o040000, metadata.id)
        repo.object_store.add_object(root)
        parent = repo.refs[b"HEAD"]
        bad = Commit()
        bad.tree = root.id
        bad.parents = [parent]
        bad.author = bad.committer = b"Test <test@example.com>"
        bad.author_time = bad.commit_time = 1
        bad.author_timezone = bad.commit_timezone = 0
        bad.message = b"bad tree"
        repo.object_store.add_object(bad)
        symbolic = repo.refs.read_ref(b"HEAD")
        assert symbolic and repo.refs.set_if_equals(symbolic[5:], parent, bad.id)
    with pytest.raises(sync.RepositorySyncError) as exc:
        await local.execute_local("diff", str(repository))
    assert exc.value.code == "unsafe_tree_path"


@pytest.mark.asyncio
async def test_commit_rejects_new_filter_attributes_before_branch_update(repository):
    with Repo(str(repository)) as repo:
        before = repo.refs[b"HEAD"]
    (repository / ".gitattributes").write_text("*.txt filter=owned\n", encoding="utf-8")
    await local.execute_local("stage", str(repository), paths=[".gitattributes"])
    with pytest.raises(sync.RepositorySyncError) as exc:
        await local.execute_local(
            "commit", str(repository), message="unsafe attributes"
        )
    assert exc.value.code == "unsafe_attributes"
    with Repo(str(repository)) as repo:
        assert repo.refs[b"HEAD"] == before


@pytest.mark.asyncio
async def test_unsafe_index_mode_is_rejected(repository):
    with Repo(str(repository)) as repo:
        index = repo.open_index(config=repo.get_config())
        blob = Blob.from_string(b"target")
        repo.object_store.add_object(blob)
        index[b"linked"] = IndexEntry(0, 0, 0, 0, 0o120000, 0, 0, 0, blob.id)
        index.write()
    with pytest.raises(sync.RepositorySyncError) as exc:
        await local.execute_local("status", str(repository))
    assert exc.value.code == "unsafe_index"


@pytest.mark.asyncio
async def test_commit_uses_local_identity_only(repository, monkeypatch):
    with Repo(str(repository)) as repo:
        config = repo.get_config()
        config.remove((b"user",), b"name")
        config.remove((b"user",), b"email")
        config.write_to_path()
    hostile = repository.parent / "global-config"
    hostile.write_text(
        "[user]\nname = Global\nemail = global@example.com\n", encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile))
    (repository / "README.md").write_text("changed\n", encoding="utf-8")
    await local.execute_local("stage", str(repository), paths=["README.md"])
    with pytest.raises(sync.RepositorySyncError) as exc:
        await local.execute_local("commit", str(repository), message="must fail")
    assert exc.value.code == "missing_identity"


@pytest.mark.asyncio
async def test_limits_and_unsupported_operations_are_rejected(repository):
    with pytest.raises(sync.RepositorySyncError):
        await local.execute_local("log", str(repository), limit=51)
    with pytest.raises(sync.RepositorySyncError):
        await local.execute_local("diff", str(repository), max_output=999)
    for action in ("switch", "merge", "reset", "delete", "push"):
        with pytest.raises(sync.RepositorySyncError) as exc:
            await local.execute_local(action, str(repository))
        assert exc.value.code == "unsupported_action"


# 2026-10-01: the diff caps were charged for every tracked file before the
# unchanged check, so a repo with large tracked media failed every diff.
def _commit_files(path, files):
    repo = Repo(str(path))
    for name, data in files.items():
        (path / name).write_bytes(data)
    porcelain.add(repo, [str(path / name) for name in files])
    porcelain.commit(
        repo,
        message=b"files",
        author=b"Test <test@example.com>",
        committer=b"Test <test@example.com>",
    )
    repo.close()


@pytest.mark.asyncio
async def test_diff_caps_apply_to_changed_files_not_the_whole_tree(repository, monkeypatch):
    _commit_files(repository, {"big1.bin": b"a" * 600, "big2.bin": b"b" * 600, "small.txt": b"x\n"})
    monkeypatch.setattr(local, "_MAX_FILE_BYTES", 1_000)
    monkeypatch.setattr(local, "_MAX_TOTAL_BYTES", 1_000)
    (repository / "small.txt").write_text("y\n", encoding="utf-8")
    result = await local.execute_local("diff", str(repository))
    assert result["changed_files"] == 1
    assert "+y" in result["diff"] and "big1.bin" not in result["diff"]


@pytest.mark.asyncio
async def test_diff_changed_oversized_file_gets_placeholder(repository, monkeypatch):
    _commit_files(repository, {"big.bin": b"a" * 600, "small.txt": b"x\n"})
    monkeypatch.setattr(local, "_MAX_FILE_BYTES", 1_000)
    (repository / "big.bin").write_bytes(b"c" * 2_000)
    (repository / "small.txt").write_text("y\n", encoding="utf-8")
    result = await local.execute_local("diff", str(repository))
    assert result["changed_files"] == 2
    assert "file too large to diff: 2000 bytes" in result["diff"]
    assert "+y" in result["diff"]


@pytest.mark.asyncio
async def test_diff_oversized_unchanged_file_is_not_reported(repository, monkeypatch):
    _commit_files(repository, {"big.bin": b"a" * 2_000})
    monkeypatch.setattr(local, "_MAX_FILE_BYTES", 1_000)
    # Force the hash path (stat can no longer vouch for the file).
    monkeypatch.setattr(local, "_stat_clean", lambda *a: False)
    result = await local.execute_local("diff", str(repository))
    if result["changed_files"] or result["diff"]:
        pytest.fail(f"changed={result['changed_files']} diff={result['diff']!r}")


@pytest.mark.asyncio
async def test_staged_diff_ignores_unchanged_large_files(repository, monkeypatch):
    _commit_files(repository, {"big.bin": b"a" * 600, "small.txt": b"x\n"})
    monkeypatch.setattr(local, "_MAX_FILE_BYTES", 1_000)
    monkeypatch.setattr(local, "_MAX_TOTAL_BYTES", 1_000)
    (repository / "small.txt").write_text("y\n", encoding="utf-8")
    await local.execute_local("stage", str(repository), paths=["small.txt"])
    result = await local.execute_local("diff", str(repository), staged=True)
    assert result["changed_files"] == 1 and "+y" in result["diff"]


@pytest.mark.asyncio
async def test_diff_detects_same_size_edit_in_racy_timestamp_tick(repository):
    """Same size, same mtime as the index entry AND as the index file: racy."""
    _commit_files(repository, {"a.txt": b"one\n"})
    target = repository / "a.txt"
    await local.execute_local("stage", str(repository), paths=["a.txt"])
    repo = Repo(str(repository))
    index_file = repo.index_path()
    tick = os.stat(index_file).st_mtime_ns
    index = repo.open_index()
    entry = index[b"a.txt"]
    entry.mtime = (tick // 1_000_000_000, tick % 1_000_000_000)
    index[b"a.txt"] = entry
    index.write()
    repo.close()
    os.utime(index_file, ns=(tick, tick))
    target.write_bytes(b"two\n")
    os.utime(target, ns=(tick, tick))
    assert target.stat().st_size == entry.size
    result = await local.execute_local("diff", str(repository))
    assert result["changed_files"] == 1 and "+two" in result["diff"]
