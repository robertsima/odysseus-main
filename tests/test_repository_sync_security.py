"""Adversarial boundaries for the narrow, non-shell repository sync service."""

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from dulwich import porcelain

from src.agent_worktree import repository_sync as rs


def _make_repo(path: Path, remote: str = "https://github.com/acme/example.git") -> Path:
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
    cfg = repo.get_config()
    head_ref = repo.refs.read_ref(b"HEAD")
    branch = head_ref.removeprefix(b"ref: refs/heads/")
    cfg.set((b"remote", b"origin"), b"url", remote.encode())
    cfg.set((b"branch", branch), b"remote", b"origin")
    cfg.set((b"branch", branch), b"merge", b"refs/heads/" + branch)
    cfg.write_to_path()
    repo.close()
    return path


@pytest.fixture
def repo_scope(tmp_path, monkeypatch):
    # Production's approved development root is intentionally below DATA_DIR.
    data = tmp_path / "data"
    root = data / "development"
    root.mkdir(parents=True)
    monkeypatch.setattr(rs, "repository_roots", lambda: (root,))
    monkeypatch.setattr(rs, "DATA_DIR", str(data))
    monkeypatch.setattr(rs, "PERSONAL_DIR", str(data / "personal_docs"))
    rs._LOCKS.clear()
    return root


def test_approved_development_repo_below_data_dir_is_not_treated_as_vault(repo_scope):
    repo = _make_repo(repo_scope / "project")
    assert rs._status_sync(repo)["repository"] == str(repo)


@pytest.mark.skipif(
    os.name == "nt", reason="symlink creation is not reliably available on Windows CI"
)
def test_repository_path_may_not_hide_a_symlink_component(repo_scope):
    real = _make_repo(repo_scope / "real")
    alias = repo_scope / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_path(alias)
    assert exc.value.code == "symlink_path"


@pytest.mark.skipif(
    os.name == "nt", reason="symlink creation is not reliably available on Windows CI"
)
@pytest.mark.parametrize(
    "relative", ["index", "refs/heads/master", "objects/info/escape"]
)
def test_symlinked_git_metadata_is_rejected_recursively(repo_scope, tmp_path, relative):
    repo = _make_repo(repo_scope / "project")
    target = repo / ".git" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    outside = tmp_path / ("outside-" + relative.replace("/", "-"))
    outside.write_bytes(b"outside")
    target.symlink_to(outside)
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_path(repo)
    assert exc.value.code == "unsafe_metadata"


def test_private_vault_stays_denied_even_when_nested_under_an_approved_root(
    repo_scope, monkeypatch
):
    private = repo_scope / "personal_docs" / "project"
    _make_repo(private)
    monkeypatch.setattr(rs, "PERSONAL_DIR", str(repo_scope / "personal_docs"))
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_path(private)
    assert exc.value.code == "private_path"


def test_git_directory_junction_is_rejected(repo_scope, monkeypatch):
    repo = _make_repo(repo_scope / "project")
    real_isjunction = getattr(os.path, "isjunction", lambda _path: False)

    def marked(path):
        return Path(path) == repo / ".git" or real_isjunction(path)

    monkeypatch.setattr(os.path, "isjunction", marked, raising=False)
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_path(repo)
    assert exc.value.code in {"unsafe_metadata", "unsupported_checkout"}


@pytest.mark.skipif(os.name == "nt", reason="hard-link behavior differs on Windows CI")
def test_mutable_git_metadata_may_not_be_hardlinked_outside_checkout(
    repo_scope, tmp_path
):
    repo = _make_repo(repo_scope / "project")
    config = repo / ".git" / "config"
    outside = tmp_path / "shared-config"
    config.replace(outside)
    os.link(outside, config)
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_path(repo)
    assert exc.value.code == "unsafe_metadata"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/../victim.git",
        "https://github.com/acme/..",
        "https://github.com./acme/example.git",
        "https://github.com@evil.example/acme/example.git",
        "https://github.com/acme/example.git?x=1",
        "file:///tmp/repository",
        "ext::sh -c id",
    ],
)
def test_remote_url_rejects_ambiguous_hosts_paths_and_transports(url):
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._https_url(url)
    assert exc.value.code == "unsupported_remote"


def test_http_transport_forces_redirects_off(monkeypatch):
    seen = {}

    def fake_request(self, method, url, *args, **kwargs):
        seen.update(kwargs)
        return object()

    import urllib3

    monkeypatch.setattr(urllib3.PoolManager, "request", fake_request)
    rs._http_pool().request("GET", "https://github.com/acme/example.git/info/refs")
    assert seen["redirect"] is False


def test_repo_config_cannot_enable_hooks_includes_or_filters(repo_scope):
    suffixes = (
        "\n[core]\n\thooksPath = ../hooks\n",
        '\n[includeIf "gitdir:~/"]\n\tpath = /tmp/evil\n',
        '\n[filter "owned"]\n\tprocess = /tmp/evil\n',
    )
    for index, suffix in enumerate(suffixes):
        repo = _make_repo(repo_scope / f"project-{index}")
        config = repo / ".git" / "config"
        config.write_text(config.read_text(encoding="utf-8") + suffix, encoding="utf-8")
        with pytest.raises(rs.RepositorySyncError) as exc:
            rs._validate_path(repo)
        assert exc.value.code == "unsafe_config"


def test_repo_config_cannot_redirect_the_worktree_outside_checkout(
    repo_scope, tmp_path
):
    repo = _make_repo(repo_scope / "project")
    config = repo / ".git" / "config"
    config.write_text(
        config.read_text(encoding="utf-8")
        + f"\n[core]\n\tworktree = {tmp_path / 'private'}\n",
        encoding="utf-8",
    )
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_path(repo)
    assert exc.value.code == "unsafe_config"


def test_existing_tracked_hardlink_is_rejected_before_status_reads_it(
    repo_scope, tmp_path
):
    repo = _make_repo(repo_scope / "project")
    tracked = repo / "README.md"
    outside = tmp_path / "private-note"
    tracked.replace(outside)
    os.link(outside, tracked)
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._status_sync(repo)
    assert exc.value.code == "unsafe_worktree"


@pytest.mark.parametrize(
    "path",
    [
        b".GIT/config",
        b"dir/.Git/index",
        b"dir/.git /owned",
        b"file:stream",
        b"CON",
        b"trailing. ",
    ],
)
def test_changed_tree_paths_are_portable_and_cannot_alias_git_metadata(tmp_path, path):
    side = SimpleNamespace(path=path)
    change = SimpleNamespace(old=None, new=side)
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._safe_changes(tmp_path, [change])
    assert exc.value.code == "unsafe_tree_path"


@pytest.mark.asyncio
async def test_canonical_path_aliases_share_one_pull_lock(repo_scope, monkeypatch):
    repo = _make_repo(repo_scope / "project")
    active = 0
    maximum = 0

    def fake_pull(repository, token=None):
        nonlocal active, maximum
        import time

        active += 1
        maximum = max(maximum, active)
        time.sleep(0.08)
        active -= 1
        return {"ok": True, "repository": str(repository)}

    monkeypatch.setattr(rs, "_pull_sync", fake_pull)
    alias = str(repo / ".." / repo.name)
    await asyncio.gather(rs.pull_repository(str(repo)), rs.pull_repository(alias))
    assert maximum == 1, "path aliases must not bypass the per-repository mutation lock"


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_release_lock_while_sync_thread_runs(
    repo_scope, monkeypatch
):
    import threading

    repo = _make_repo(repo_scope / "project")
    entered = threading.Event()
    release = threading.Event()
    active = 0
    maximum = 0

    def blocked_pull(repository, token=None):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        entered.set()
        release.wait(2)
        active -= 1
        return {"ok": True}

    monkeypatch.setattr(rs, "_pull_sync", blocked_pull)
    first = asyncio.create_task(rs.pull_repository(repo))
    assert await asyncio.to_thread(entered.wait, 1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(rs.pull_repository(repo))
    await asyncio.sleep(0.08)
    try:
        assert maximum == 1, (
            "cancelling await must not unlock a still-running mutation thread"
        )
    finally:
        release.set()
        await second
        await asyncio.sleep(0.02)


def test_git_attribute_drivers_are_rejected_before_checkout(repo_scope):
    repo_path = _make_repo(repo_scope / "project")
    (repo_path / ".gitattributes").write_text("*.txt filter=owned\n", encoding="utf-8")
    repo = porcelain.open_repo(str(repo_path))
    porcelain.add(repo, [".gitattributes"])
    porcelain.commit(
        repo,
        message=b"attributes",
        author=b"Test <test@example.com>",
        committer=b"Test <test@example.com>",
    )
    head = repo.refs[b"HEAD"]
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_tree(repo, head)
    assert exc.value.code == "unsafe_attributes"
    repo.close()


@pytest.mark.skipif(
    os.name == "nt", reason="repository symlink entries require Unix semantics"
)
def test_remote_tree_symlinks_are_rejected_before_checkout(repo_scope):
    repo_path = _make_repo(repo_scope / "project")
    os.symlink("../outside", repo_path / "escape")
    repo = porcelain.open_repo(str(repo_path))
    porcelain.add(repo, ["escape"])
    porcelain.commit(
        repo,
        message=b"symlink",
        author=b"Test <test@example.com>",
        committer=b"Test <test@example.com>",
    )
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._validate_tree(repo, repo.refs[b"HEAD"])
    assert exc.value.code == "unsafe_tree"
    repo.close()


def test_failed_checkout_rolls_back_files_index_and_branch(repo_scope, monkeypatch):
    repo_path = _make_repo(repo_scope / "project")
    repo = porcelain.open_repo(str(repo_path))
    before = repo.refs[b"HEAD"]
    (repo_path / "README.md").write_text("two\n", encoding="utf-8")
    porcelain.add(repo, ["README.md"])
    after = porcelain.commit(
        repo,
        message=b"remote update",
        author=b"Test <test@example.com>",
        committer=b"Test <test@example.com>",
    )
    porcelain.reset(repo, "hard", before)
    repo.close()

    class Client:
        def fetch(self, _path, _repo, determine_wants):
            refs = {b"refs/heads/main": after, b"refs/heads/master": after}
            determine_wants(refs)
            return SimpleNamespace(refs=refs)

    monkeypatch.setattr(
        rs,
        "get_transport_and_path_from_url",
        lambda *args, **kwargs: (Client(), "acme/example.git"),
    )

    def partial_checkout(*args, **kwargs):
        # Bytes equal the fetched blob are safe to identify as the sync's own
        # partial write and restore from the bounded snapshot.
        (repo_path / "README.md").write_bytes(b"two\n")
        raise OSError("simulated checkout failure")

    monkeypatch.setattr(rs, "update_working_tree", partial_checkout)
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._pull_sync(repo_path)
    assert exc.value.code == "checkout_failed"

    reopened = porcelain.open_repo(str(repo_path))
    assert reopened.refs[b"HEAD"] == before
    assert (repo_path / "README.md").read_text(encoding="utf-8") == "one\n"
    assert rs._status(reopened)["clean"] is True
    reopened.close()


def test_checkout_failure_preserves_concurrent_edit_and_requires_recovery(
    repo_scope, monkeypatch
):
    repo_path = _make_repo(repo_scope / "project")
    repo = porcelain.open_repo(str(repo_path))
    before = repo.refs[b"HEAD"]
    (repo_path / "README.md").write_text("two\n", encoding="utf-8")
    porcelain.add(repo, ["README.md"])
    after = porcelain.commit(
        repo,
        message=b"remote update",
        author=b"Test <test@example.com>",
        committer=b"Test <test@example.com>",
    )
    porcelain.reset(repo, "hard", before)
    repo.close()

    class Client:
        def fetch(self, _path, _repo, determine_wants):
            refs = {b"refs/heads/main": after, b"refs/heads/master": after}
            determine_wants(refs)
            return SimpleNamespace(refs=refs)

    monkeypatch.setattr(
        rs,
        "get_transport_and_path_from_url",
        lambda *args, **kwargs: (Client(), "acme/example.git"),
    )

    def concurrent_failure(*args, **kwargs):
        target = repo_path / "README.md"
        target.write_text("third-party edit\n", encoding="utf-8")
        raise OSError("checkout raced another writer")

    monkeypatch.setattr(rs, "update_working_tree", concurrent_failure)
    with pytest.raises(rs.RepositorySyncError) as exc:
        rs._pull_sync(repo_path)
    assert exc.value.code == "recovery_required"
    assert (repo_path / "README.md").read_text(encoding="utf-8") == "third-party edit\n"
