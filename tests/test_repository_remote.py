from pathlib import Path
from types import SimpleNamespace

import pytest
from dulwich import porcelain

from src.agent_worktree import repository_remote as rr
from src.agent_worktree import repository_sync as rs


def _repo(path: Path):
    path.mkdir(parents=True)
    repo = porcelain.init(str(path))
    (path / "a.txt").write_bytes(b"one\n")
    porcelain.add(repo, ["a.txt"])
    porcelain.commit(repo, message=b"one", author=b"T <t@e>", committer=b"T <t@e>")
    head_ref = repo.refs.read_ref(b"HEAD")
    branch = head_ref.removeprefix(b"ref: refs/heads/")
    cfg = repo.get_config()
    cfg.set((b"remote", b"origin"), b"url", b"https://github.com/acme/project.git")
    cfg.set((b"branch", branch), b"remote", b"origin")
    cfg.set((b"branch", branch), b"merge", b"refs/heads/" + branch)
    cfg.write_to_path()
    repo.close()
    return path, branch


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "development"
    path, branch = _repo(root / "project")
    monkeypatch.setattr(rs, "repository_roots", lambda: (root,))
    monkeypatch.setattr(rs, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(rs, "PERSONAL_DIR", str(tmp_path / "personal"))
    monkeypatch.setattr(rs, "vault_root", lambda: str(tmp_path / "vault"))
    return path, branch


@pytest.mark.asyncio
async def test_unknown_and_history_rewrite_actions_are_explicitly_unsupported(checkout):
    path, _ = checkout
    for action in ("reset", "rebase", "force_push", "delete_remote_branch", "anything"):
        with pytest.raises(rs.RepositorySyncError) as exc:
            await rr.execute_remote(action, path)
        assert exc.value.code == "unsupported_action"


@pytest.mark.asyncio
async def test_pull_delegates_to_hardened_sync(checkout, monkeypatch):
    path, _ = checkout
    seen = {}

    async def pull(repository, token=None):
        seen.update(repository=repository, token=token)
        return {"ok": True}

    monkeypatch.setattr(rr, "pull_repository", pull)
    assert await rr.execute_remote("pull", path, token="secret") == {"ok": True}
    assert seen == {"repository": path, "token": "secret"}


def test_delete_branch_requires_fresh_approved_head_and_target(checkout):
    path, branch = checkout
    repo = porcelain.open_repo(str(path))
    head = repo.refs[b"HEAD"]
    old = head
    repo.refs[b"refs/heads/topic"] = old
    repo.close()

    with pytest.raises(rs.RepositorySyncError) as exc:
        rr._delete_branch_sync(path, "topic", "0" * 40, old.decode())
    assert exc.value.code == "stale_approval"
    with pytest.raises(rs.RepositorySyncError) as exc:
        rr._delete_branch_sync(path, "topic", head.decode(), "f" * 40)
    assert exc.value.code == "stale_approval"
    assert (
        rr._delete_branch_sync(path, "topic", head.decode(), old.decode())["branch"]
        == "topic"
    )


def test_delete_branch_refuses_current_and_unmerged(checkout):
    path, branch = checkout
    repo = porcelain.open_repo(str(path))
    head = repo.refs[b"HEAD"]
    repo.close()
    with pytest.raises(rs.RepositorySyncError) as exc:
        rr._delete_branch_sync(path, branch.decode(), head.decode(), head.decode())
    assert exc.value.code == "current_branch"

    repo = porcelain.open_repo(str(path))
    repo.refs[b"refs/heads/topic"] = head
    repo.refs[b"HEAD"] = head
    (path / "a.txt").write_bytes(b"topic\n")
    porcelain.add(repo, ["a.txt"])
    topic = porcelain.commit(
        repo, message=b"topic", author=b"T <t@e>", committer=b"T <t@e>"
    )
    repo.refs[b"refs/heads/" + branch] = head
    repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/" + branch)
    porcelain.reset(repo, "hard", head)
    repo.refs[b"refs/heads/topic"] = topic
    repo.close()
    with pytest.raises(rs.RepositorySyncError) as exc:
        rr._delete_branch_sync(path, "topic", head.decode(), topic.decode())
    assert exc.value.code == "unmerged_branch"


def test_push_stale_approval_blocks_before_transport(checkout, monkeypatch):
    path, _ = checkout
    monkeypatch.setattr(rr, "is_odysseus_repository", lambda _path: False)
    monkeypatch.setattr(rr, "_client", lambda *a: pytest.fail("network touched"))
    with pytest.raises(rs.RepositorySyncError) as exc:
        rr._push_sync(path, expected_head="0" * 40)
    assert exc.value.code == "stale_approval"


def test_push_refuses_odysseus_repository_before_network(checkout, monkeypatch):
    path, _ = checkout
    monkeypatch.setattr(rr, "is_odysseus_repository", lambda _path: True)
    monkeypatch.setattr(rr, "_client", lambda *a: pytest.fail("network touched"))
    with pytest.raises(rs.RepositorySyncError) as exc:
        rr._push_sync(path, expected_head="0" * 40)
    assert exc.value.code == "use_publish_flow"


def test_fetch_updates_only_remote_tracking_ref(checkout, monkeypatch):
    path, branch = checkout
    repo = porcelain.open_repo(str(path))
    local_before = repo.refs[b"HEAD"]
    (path / "a.txt").write_bytes(b"remote\n")
    porcelain.add(repo, ["a.txt"])
    remote_oid = porcelain.commit(
        repo, message=b"remote", author=b"T <t@e>", committer=b"T <t@e>"
    )
    porcelain.reset(repo, "hard", local_before)
    repo.close()

    monkeypatch.setattr(rr, "_fetch_exact", lambda repo, url, ref, token: remote_oid)
    result = rr._fetch_sync(path)
    reopened = porcelain.open_repo(str(path))
    assert reopened.refs[b"HEAD"] == local_before
    assert reopened.refs[b"refs/remotes/origin/" + branch] == remote_oid
    assert result["updated"] is True
    reopened.close()


def test_remote_branch_rejects_refs_and_option_shapes():
    for value in ("refs/heads/main", "--force", "../main", ""):
        with pytest.raises(rs.RepositorySyncError) as exc:
            rr._branch_name(value, field="remote_branch")
        assert exc.value.code == "invalid_branch"


def test_switch_existing_local_branch_and_refuse_dirty(checkout):
    path, current = checkout
    repo = porcelain.open_repo(str(path))
    head = repo.refs[b"HEAD"]
    repo.refs[b"refs/heads/topic"] = head
    repo.close()
    result = rr._switch_sync(path, "topic")
    assert result["branch"] == "topic"
    repo = porcelain.open_repo(str(path))
    assert repo.refs.read_ref(b"HEAD") == b"ref: refs/heads/topic"
    repo.close()
    (path / "a.txt").write_bytes(b"dirty\n")
    with pytest.raises(rs.RepositorySyncError) as exc:
        rr._switch_sync(path, current.decode())
    assert exc.value.code == "dirty_tree"


def test_merge_is_fast_forward_only_and_approval_bound(checkout, monkeypatch):
    path, branch = checkout
    repo = porcelain.open_repo(str(path))
    before = repo.refs[b"HEAD"]
    repo.refs[b"refs/heads/topic"] = before
    repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/topic")
    (path / "a.txt").write_bytes(b"two\n")
    porcelain.add(repo, ["a.txt"])
    target = porcelain.commit(
        repo, message=b"two", author=b"T <t@e>", committer=b"T <t@e>"
    )
    repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/" + branch)
    porcelain.reset(repo, "hard", before)
    repo.close()

    with pytest.raises(rs.RepositorySyncError) as exc:
        rr._merge_sync(path, "topic", before.decode(), "f" * 40)
    assert exc.value.code == "stale_approval"

    def checkout_ff(_path, repo, old, new, *, update_ref=None, new_symbolic_head=None):
        assert old == before and new == target and update_ref == b"refs/heads/" + branch
        assert repo.refs.set_if_equals(update_ref, old, new)

    monkeypatch.setattr(rr, "checkout_commit", checkout_ff)
    result = rr._merge_sync(path, "topic", before.decode(), target.decode())
    assert result["after"] == target.decode() and result["updated"] is True


def test_merge_local_branch_does_not_require_current_branch_upstream(
    checkout, monkeypatch
):
    path, branch = checkout
    repo = porcelain.open_repo(str(path))
    before = repo.refs[b"HEAD"]
    repo.refs[b"refs/heads/topic"] = before
    cfg = repo.get_config()
    cfg.remove((b"branch", branch), b"remote")
    cfg.remove((b"branch", branch), b"merge")
    cfg.write_to_path()
    repo.close()
    monkeypatch.setattr(rr, "checkout_commit", lambda *a, **k: None)
    result = rr._merge_sync(path, "topic", before.decode(), before.decode())
    assert result["ref"] == "topic"


def test_delete_branch_compare_and_delete_detects_retarget(checkout, monkeypatch):
    path, _ = checkout
    repo = porcelain.open_repo(str(path))
    head = repo.refs[b"HEAD"]
    repo.refs[b"refs/heads/topic"] = head
    refs_type = type(repo.refs)
    repo.close()
    monkeypatch.setattr(refs_type, "remove_if_equals", lambda *a, **k: False)
    with pytest.raises(rs.RepositorySyncError) as exc:
        rr._delete_branch_sync(path, "topic", head.decode(), head.decode())
    assert exc.value.code == "stale_branch"


def test_first_push_new_branch_uses_configured_origin_and_explicit_remote_branch(
    checkout, monkeypatch
):
    path, branch = checkout
    repo = porcelain.open_repo(str(path))
    cfg = repo.get_config()
    cfg.remove((b"branch", branch), b"remote")
    cfg.remove((b"branch", branch), b"merge")
    cfg.write_to_path()
    head = repo.refs[b"HEAD"]
    repo.close()
    monkeypatch.setattr(rr, "is_odysseus_repository", lambda _path: False)
    monkeypatch.setattr(rr, "load_config", lambda: SimpleNamespace(repo_slug=""))
    monkeypatch.setattr(
        rr,
        "_fetch_exact",
        lambda *a, **k: (_ for _ in ()).throw(
            rs.RepositorySyncError("missing_remote_branch", "missing")
        ),
    )

    class Client:
        def send_pack(self, path, update_refs, generate_pack_data, atomic=False):
            assert path == b"acme/project.git" and atomic is True
            assert update_refs({}) == {b"refs/heads/new-topic": head}
            return SimpleNamespace(ref_status={})

        def close(self):
            pass

    monkeypatch.setattr(
        rr, "_client", lambda url, token: (Client(), "acme/project.git")
    )
    result = rr._push_sync(path, remote_branch="new-topic", expected_head=head.decode())
    assert result["remote_branch"] == "new-topic"
    assert result["needs_upstream_action"] is True
    configured = rr._set_upstream_sync(path, remote_branch="new-topic")
    assert configured["upstream_configured"] is True
    repo = porcelain.open_repo(str(path))
    _local, _branch, remote, merge_ref, _url = rs._branch_upstream(repo)
    assert remote == "origin" and merge_ref == b"refs/heads/new-topic"
    repo.close()


def test_app_remote_identity_guard_applies_outside_configured_checkout(
    checkout, monkeypatch
):
    path, _ = checkout
    repo = porcelain.open_repo(str(path))
    head = repo.refs[b"HEAD"]
    repo.close()
    monkeypatch.setattr(rr, "is_odysseus_repository", lambda _path: False)
    monkeypatch.setattr(
        rr, "load_config", lambda: SimpleNamespace(repo_slug="acme/project")
    )
    monkeypatch.setattr(rr, "_client", lambda *a: pytest.fail("network touched"))
    with pytest.raises(rs.RepositorySyncError) as exc:
        rr._push_sync(path, expected_head=head.decode())
    assert exc.value.code == "use_publish_flow"


def test_set_upstream_requires_existing_tracking_ref_and_never_replaces(checkout):
    path, branch = checkout
    repo = porcelain.open_repo(str(path))
    cfg = repo.get_config()
    cfg.remove((b"branch", branch), b"remote")
    cfg.remove((b"branch", branch), b"merge")
    cfg.write_to_path()
    head = repo.refs[b"HEAD"]
    repo.close()
    with pytest.raises(rs.RepositorySyncError) as exc:
        rr._set_upstream_sync(path, remote_branch="missing")
    assert exc.value.code == "missing_tracking_ref"

    repo = porcelain.open_repo(str(path))
    repo.refs[b"refs/remotes/origin/topic"] = head
    repo.close()
    assert (
        rr._set_upstream_sync(path, remote_branch="topic")["upstream_configured"]
        is True
    )
    with pytest.raises(rs.RepositorySyncError) as exc:
        rr._set_upstream_sync(path, remote_branch="other")
    assert exc.value.code == "upstream_exists"


def test_set_upstream_honors_config_lock(checkout):
    path, branch = checkout
    repo = porcelain.open_repo(str(path))
    cfg = repo.get_config()
    cfg.remove((b"branch", branch), b"remote")
    cfg.remove((b"branch", branch), b"merge")
    cfg.write_to_path()
    repo.refs[b"refs/remotes/origin/topic"] = repo.refs[b"HEAD"]
    repo.close()
    lock = path / ".git" / "config.lock"
    lock.write_bytes(b"busy")
    try:
        with pytest.raises(rs.RepositorySyncError) as exc:
            rr._set_upstream_sync(path, remote_branch="topic")
        assert exc.value.code == "config_busy"
    finally:
        lock.unlink(missing_ok=True)
