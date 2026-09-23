from pathlib import Path

import pytest
from dulwich import porcelain

from src.agent_worktree import repository_sync as rs


def make_repo(path: Path):
    path.mkdir()
    repo = porcelain.init(str(path))
    (path / "README.md").write_text("one\n", encoding="utf-8")
    porcelain.add(repo, ["README.md"])
    porcelain.commit(
        repo,
        message=b"initial",
        author=b"Test <test@example.com>",
        committer=b"Test <test@example.com>",
    )
    cfg = repo.get_config()
    branch = repo.refs.read_ref(b"HEAD")[len(b"ref: refs/heads/") :]
    cfg.set((b"remote", b"origin"), b"url", b"https://github.com/acme/example.git")
    cfg.set((b"branch", branch), b"remote", b"origin")
    cfg.set((b"branch", branch), b"merge", b"refs/heads/" + branch)
    cfg.write_to_path()
    repo.close()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    path = root / "project"
    make_repo(path)
    monkeypatch.setattr(rs, "repository_roots", lambda: (root,))
    monkeypatch.setattr(rs, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(rs, "PERSONAL_DIR", str(tmp_path / "data" / "personal_docs"))
    return path


@pytest.mark.asyncio
async def test_status_reports_clean_then_dirty(checkout):
    clean = await rs.repository_status(str(checkout))
    assert clean["dirty"]["clean"] is True and clean["branch"] in {"main", "master"}
    (checkout / "README.md").write_text("changed\n", encoding="utf-8")
    dirty = await rs.repository_status(str(checkout))
    assert (
        dirty["dirty"]["clean"] is False and "README.md" in dirty["dirty"]["unstaged"]
    )


@pytest.mark.asyncio
async def test_list_only_returns_supported_physical_checkouts(checkout):
    rows = await rs.list_repositories()
    assert [row["repository"] for row in rows] == [str(checkout)]


def test_scp_github_remote_is_normalized_without_mutating_config(checkout):
    cfg_path = checkout / ".git" / "config"
    original = cfg_path.read_text(encoding="utf-8").replace(
        "https://github.com/acme/example.git", "git@github.com:acme/example.git"
    )
    cfg_path.write_text(original, encoding="utf-8")
    result = rs._status_sync(checkout)
    assert result["remote_url"] == "https://github.com/acme/example.git"
    assert "git@github.com:" in cfg_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_pull_refuses_dirty_before_transport(checkout, monkeypatch):
    (checkout / "new.txt").write_text("untracked", encoding="utf-8")
    monkeypatch.setattr(
        rs,
        "get_transport_and_path_from_url",
        lambda *a, **k: pytest.fail("network touched"),
    )
    with pytest.raises(rs.RepositorySyncError) as exc:
        await rs.pull_repository(checkout)
    assert exc.value.code == "dirty_tree"


def test_rejects_includes_filters_and_non_github(checkout):
    cfg = checkout / ".git" / "config"
    cfg.write_text(
        cfg.read_text(encoding="utf-8") + "\n[include]\npath = ../evil\n",
        encoding="utf-8",
    )
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._status_sync(checkout)
    assert exc.value.code == "unsafe_config"
    assert (
        rs._https_url("https://github.com/acme/example.git")
        == "https://github.com/acme/example.git"
    )
    with pytest.raises(rs.RepositorySyncError):
        rs._https_url("https://gitlab.com/acme/example.git")


def test_rejects_linked_worktree_metadata(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    path = root / "project"
    path.mkdir(parents=True)
    (path / ".git").write_text("gitdir: ../elsewhere", encoding="utf-8")
    monkeypatch.setattr(rs, "repository_roots", lambda: (root,))
    monkeypatch.setattr(rs, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(rs, "PERSONAL_DIR", str(tmp_path / "data" / "personal_docs"))
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_path(path)
    assert exc.value.code == "unsupported_checkout"


def test_rejects_shared_and_sparse_metadata(checkout):
    gitdir = checkout / ".git"
    (gitdir / "commondir").write_text("../outside\n", encoding="utf-8")
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_path(checkout)
    assert exc.value.code == "unsafe_metadata"
    (gitdir / "commondir").unlink()
    (gitdir / "info").mkdir(exist_ok=True)
    (gitdir / "info" / "sparse-checkout").write_text("/*\n", encoding="utf-8")
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_path(checkout)
    assert exc.value.code == "unsafe_metadata"


def test_repo_local_gitignore_is_honored_without_global_config(checkout):
    (checkout / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (checkout / "ignored").mkdir()
    (checkout / "ignored" / "large.tmp").write_text("ignored", encoding="utf-8")
    repo = porcelain.open_repo(str(checkout))
    porcelain.add(repo, [".gitignore"])
    porcelain.commit(
        repo,
        message=b"ignore",
        author=b"Test <test@example.com>",
        committer=b"Test <test@example.com>",
    )
    status = rs._status(repo)
    repo.close()
    assert status["clean"] is True


def test_checkout_refuses_ignored_untracked_collision_and_preserves_bytes(checkout):
    ignore = checkout / ".gitignore"
    ignore.write_text(".env\n", encoding="utf-8")
    repo = porcelain.open_repo(str(checkout))
    porcelain.add(repo, [".gitignore"])
    old = porcelain.commit(
        repo,
        message=b"ignore env",
        author=b"Test <test@example.com>",
        committer=b"Test <test@example.com>",
    )

    # Build a fetched-like descendant that adds the ignored path without
    # using network transport. Temporarily hide the ignore file only while
    # adding; it remains tracked in the resulting commit.
    hidden = checkout / ".gitignore.hidden"
    ignore.replace(hidden)
    (checkout / ".env").write_bytes(b"REMOTE=value\n")
    porcelain.add(repo, [".env"])
    hidden.replace(ignore)
    target = porcelain.commit(
        repo,
        message=b"remote adds env",
        author=b"Test <test@example.com>",
        committer=b"Test <test@example.com>",
    )
    porcelain.reset(repo, "hard", old)

    private_bytes = b"LOCAL_PRIVATE=must-survive\n"
    (checkout / ".env").write_bytes(private_bytes)
    assert rs._status(repo)["clean"] is True  # ignored, but still protected
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs.checkout_commit(checkout, repo, old, target)
    assert exc.value.code == "untracked_collision"
    assert (checkout / ".env").read_bytes() == private_bytes
    assert repo.refs[b"HEAD"] == old
    repo.close()
