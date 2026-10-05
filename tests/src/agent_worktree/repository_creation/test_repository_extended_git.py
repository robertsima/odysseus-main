from pathlib import Path

import pytest
from dulwich import porcelain

from src.agent_worktree import repository_history as rh
from src.agent_worktree import repository_local as rl
from src.agent_worktree import repository_sync as rs


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "development"
    path = root / "project"
    path.mkdir(parents=True)
    repo = porcelain.init(str(path))
    (path / "a.txt").write_bytes(b"one\n")
    porcelain.add(repo, ["a.txt"])
    porcelain.commit(repo, message=b"one", author=b"T <t@e>", committer=b"T <t@e>")
    repo.close()
    monkeypatch.setattr(rs, "repository_roots", lambda: (root,))
    monkeypatch.setattr(rs, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(rs, "PERSONAL_DIR", str(tmp_path / "personal"))
    monkeypatch.setattr(rs, "vault_root", lambda: str(tmp_path / "vault"))
    return path


def test_managed_stash_round_trip_preserves_staged_state(checkout):
    (checkout / "a.txt").write_bytes(b"staged\n")
    with porcelain.open_repo(str(checkout)) as repo:
        porcelain.add(repo, ["a.txt"])
    created = rh._stash_create_sync(checkout, "test")
    assert (checkout / "a.txt").read_bytes() == b"one\n"
    applied = rh._stash_apply_sync(checkout, 0, created["stash"])
    assert applied["status"]["staged"] == ["a.txt"]
    assert rh._stash_list_sync(checkout)["stashes"][0]["id"] == created["stash"]


def test_managed_stash_refuses_changed_base(checkout):
    (checkout / "a.txt").write_bytes(b"local\n")
    created = rh._stash_create_sync(checkout)
    with porcelain.open_repo(str(checkout)) as repo:
        (checkout / "b.txt").write_bytes(b"two\n")
        porcelain.add(repo, ["b.txt"])
        porcelain.commit(repo, message=b"two", author=b"T <t@e>", committer=b"T <t@e>")
    with pytest.raises(rs.RepositorySyncError) as exc:
        rh._stash_apply_sync(checkout, 0, created["stash"])
    assert exc.value.code == "stash_base_changed"


def test_clean_reset_leaves_recovery_ref(checkout):
    with porcelain.open_repo(str(checkout)) as repo:
        before = repo.refs[b"HEAD"]
        (checkout / "a.txt").write_bytes(b"two\n")
        porcelain.add(repo, ["a.txt"])
        head = porcelain.commit(
            repo, message=b"two", author=b"T <t@e>", committer=b"T <t@e>"
        )
    result = rh._reset_sync(checkout, before.decode(), head.decode(), before.decode())
    assert result["after"] == before.decode()
    with porcelain.open_repo(str(checkout)) as repo:
        assert repo.refs[result["recovery_ref"].encode()] == head


def test_noninteractive_rebase_rewrites_and_leaves_recovery_ref(checkout):
    with porcelain.open_repo(str(checkout)) as repo:
        base = repo.refs[b"HEAD"]
        main_ref = repo.refs.read_ref(b"HEAD")[5:]
        topic_ref = b"refs/heads/topic"
        repo.refs[topic_ref] = base
        repo.refs.set_symbolic_ref(b"HEAD", topic_ref)
        (checkout / "topic.txt").write_bytes(b"topic\n")
        porcelain.add(repo, ["topic.txt"])
        topic = porcelain.commit(
            repo, message=b"topic", author=b"T <t@e>", committer=b"T <t@e>"
        )
        repo.refs.set_symbolic_ref(b"HEAD", main_ref)
        porcelain.reset(repo, "hard", base)
        (checkout / "upstream.txt").write_bytes(b"upstream\n")
        porcelain.add(repo, ["upstream.txt"])
        upstream = porcelain.commit(
            repo, message=b"upstream", author=b"T <t@e>", committer=b"T <t@e>"
        )
        repo.refs.set_symbolic_ref(b"HEAD", topic_ref)
        porcelain.reset(repo, "hard", topic)
        main_name = main_ref.removeprefix(b"refs/heads/").decode()
    result = rh._rebase_sync(checkout, main_name, topic.decode(), upstream.decode())
    assert result["before"] == topic.decode()
    assert result["after"] != topic.decode()
    with porcelain.open_repo(str(checkout)) as repo:
        assert repo.refs[result["recovery_ref"].encode()] == topic
        assert repo.refs[b"HEAD"] == result["after"].encode()


def test_init_repository_supports_first_stage_and_commit(tmp_path, monkeypatch):
    from src.agent_worktree.repository_creation import _init_sync

    root = tmp_path / "development"
    root.mkdir()
    path = root / "new"
    monkeypatch.setattr(rs, "repository_roots", lambda: (root,))
    monkeypatch.setattr(rs, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(rs, "PERSONAL_DIR", str(tmp_path / "personal"))
    monkeypatch.setattr(rs, "vault_root", lambda: str(tmp_path / "vault"))
    target = rs._validate_new_path(path, allow_empty_directory=True)
    assert _init_sync(target)["unborn"] is True
    (path / "README.md").write_text("hello\n")
    rl._operate(path, "stage", {"paths": ["README.md"]})
    committed = rl._operate(
        path,
        "commit",
        {"message": "initial", "author_name": "T", "author_email": "t@e"},
    )
    assert len(committed["commit"]) == 40
    assert rl._operate(path, "status", {})["unborn"] is False


def test_clone_uses_authenticated_client_and_validates_checkout(tmp_path, monkeypatch):
    from src.agent_worktree import repository_creation as rc

    root = tmp_path / "development"
    root.mkdir()
    target = root / "clone"
    monkeypatch.setattr(rs, "repository_roots", lambda: (root,))
    monkeypatch.setattr(rs, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(rs, "PERSONAL_DIR", str(tmp_path / "personal"))
    monkeypatch.setattr(rs, "vault_root", lambda: str(tmp_path / "vault"))
    seen = {}

    class Client:
        def clone(self, remote_path, target_path, **kwargs):
            seen.update(remote_path=remote_path, kwargs=kwargs)
            path = Path(target_path)
            path.mkdir()
            repo = porcelain.init(str(path))
            (path / "README.md").write_bytes(b"hello\n")
            porcelain.add(repo, ["README.md"])
            porcelain.commit(
                repo, message=b"initial", author=b"T <t@e>", committer=b"T <t@e>"
            )
            head_ref = repo.refs.read_ref(b"HEAD")[5:]
            branch = head_ref.removeprefix(b"refs/heads/")
            cfg = repo.get_config()
            cfg.set(
                (b"remote", b"origin"), b"url", b"https://github.com/acme/project.git"
            )
            cfg.set((b"branch", branch), b"remote", b"origin")
            cfg.set((b"branch", branch), b"merge", b"refs/heads/" + branch)
            cfg.write_to_path()
            return repo

        def close(self):
            pass

    monkeypatch.setattr(
        rc,
        "_client",
        lambda url, token: (
            seen.update(url=url, token=token) or Client(),
            "acme/project.git",
        ),
    )
    result = rc._clone_sync(
        target,
        source="https://github.com/acme/project.git",
        token="ghp_test",
        depth=1,
    )
    assert result["action"] == "clone" and result["head"]
    assert seen["token"] == "ghp_test"
    assert seen["kwargs"]["checkout"] is False
