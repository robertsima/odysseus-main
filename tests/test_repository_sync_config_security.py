"""Repository sync must not inherit ambient Git configuration or layouts."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from dulwich import porcelain
from dulwich.config import StackedConfig

from src.agent_worktree import repository_sync as rs


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    repo = porcelain.init(str(path))
    (path / "README.md").write_text("one\n", encoding="utf-8")
    porcelain.add(repo, ["README.md"])
    porcelain.commit(
        repo,
        message=b"initial",
        author=b"Test <test@example.com>",
        committer=b"Test <test@example.com>",
    )
    config = repo.get_config()
    branch = repo.refs.read_ref(b"HEAD")[len(b"ref: refs/heads/") :]
    config.set((b"remote", b"origin"), b"url", b"https://github.com/acme/example.git")
    config.set((b"branch", branch), b"remote", b"origin")
    config.set((b"branch", branch), b"merge", b"refs/heads/" + branch)
    config.write_to_path()
    repo.close()
    return path


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    repository = _make_repo(root / "project")
    monkeypatch.setattr(rs, "repository_roots", lambda: (root,))
    monkeypatch.setattr(rs, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(rs, "PERSONAL_DIR", str(tmp_path / "data" / "personal_docs"))
    return repository


def test_physical_checkout_rejects_commondir_escape(checkout, tmp_path):
    outside = tmp_path / "outside-git"
    outside.mkdir()
    (checkout / ".git" / "commondir").write_text(str(outside), encoding="utf-8")

    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_path(checkout)

    assert exc.value.code in {
        "unsupported_checkout",
        "unsafe_metadata",
        "unsafe_config",
    }


def test_worktree_config_extension_is_rejected(checkout):
    config = checkout / ".git" / "config"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "\n[extensions]\n\tworktreeConfig = true\n",
        encoding="utf-8",
    )
    (checkout / ".git" / "config.worktree").write_text(
        "[core]\n\texcludesFile = /outside/secrets\n", encoding="utf-8"
    )

    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_path(checkout)

    assert exc.value.code in {"unsafe_config", "unsafe_metadata"}


@pytest.mark.parametrize(
    "setting",
    ["sparseCheckout = true", "sparseCheckoutCone = true", "worktree = /outside"],
)
def test_redirecting_or_sparse_core_config_is_rejected(checkout, setting):
    config = checkout / ".git" / "config"
    config.write_text(
        config.read_text(encoding="utf-8") + f"\n[core]\n\t{setting}\n",
        encoding="utf-8",
    )

    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_path(checkout)

    assert exc.value.code == "unsafe_config"


def test_sparse_index_is_rejected_before_status_helpers():
    class SparseIndex:
        def is_sparse(self):
            return True

    repo = SimpleNamespace(
        get_config=lambda: SimpleNamespace(),
        open_index=lambda config=None: SparseIndex(),
    )

    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._status(repo)

    assert exc.value.code == "unsupported_sparse_checkout"


def test_status_never_loads_global_or_system_git_config(checkout, monkeypatch):
    monkeypatch.setattr(
        StackedConfig,
        "default_backends",
        classmethod(lambda cls: pytest.fail("ambient Git config was loaded")),
    )

    result = rs._status_sync(checkout)

    assert result["dirty"]["clean"] is True


def test_hostile_global_excludes_file_is_not_read_or_applied(
    checkout, tmp_path, monkeypatch
):
    outside_excludes = tmp_path / "outside-excludes"
    outside_excludes.write_text("hidden.txt\n", encoding="utf-8")
    global_config = tmp_path / "hostile.gitconfig"
    global_config.write_text(
        f"[core]\n\texcludesFile = {outside_excludes.as_posix()}\n", encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    (checkout / "hidden.txt").write_text("must remain visible\n", encoding="utf-8")

    from dulwich import ignore as dulwich_ignore

    original = dulwich_ignore.IgnoreFilter.from_path

    def guarded_from_path(path, *args, **kwargs):
        assert Path(path).resolve() != outside_excludes.resolve(), (
            "outside excludes file was read"
        )
        return original(path, *args, **kwargs)

    monkeypatch.setattr(dulwich_ignore.IgnoreFilter, "from_path", guarded_from_path)

    result = rs._status_sync(checkout)

    assert "hidden.txt" in result["dirty"]["untracked"]


def test_spaced_include_section_is_rejected_without_opening_target(checkout, tmp_path):
    outside = tmp_path / "outside-config"
    outside.write_text("[core]\nworktree = /private\n", encoding="utf-8")
    config = checkout / ".git" / "config"
    config.write_text(
        config.read_text(encoding="utf-8")
        + f"\n[ include ]\npath = {outside.as_posix()}\n",
        encoding="utf-8",
    )
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_path(checkout)
    assert exc.value.code == "unsafe_config"


def test_status_rejects_unsafe_index_name_before_content_helpers(monkeypatch):
    class Index:
        def is_sparse(self):
            return False

        def __len__(self):
            return 1

        def __iter__(self):
            return iter([b"C:/outside/private"])

    repo = SimpleNamespace(
        path="/approved/repo",
        get_config=lambda: SimpleNamespace(),
        open_index=lambda config=None: Index(),
    )
    monkeypatch.setattr(
        rs, "get_tree_changes", lambda *a, **k: pytest.fail("content helper ran")
    )
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._status(repo)
    assert exc.value.code == "unsafe_tree_path"


@pytest.mark.skipif(
    not hasattr(__import__("os"), "link"), reason="hard links unavailable"
)
def test_linked_gitignore_is_rejected_before_ignore_manager_reads_it(
    checkout, tmp_path
):
    import os

    outside = tmp_path / "outside-ignore"
    outside.write_text("private/*\n", encoding="utf-8")
    os.link(outside, checkout / ".gitignore")
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._status_sync(checkout)
    assert exc.value.code == "unsafe_ignore"
