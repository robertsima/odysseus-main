"""manage_git in a worker's workspace, and on a GitHub Enterprise host.

A headless worker's file tools were confined to its workspace while manage_git
refused that same checkout as outside the configured roots. And manage_git's
GitHub credential was public-host only, so on an Enterprise install (the host
the GitHub MCP server already used) every clone and fetch went anonymous.
"""
import base64
from contextlib import contextmanager

import pytest
from dulwich.repo import Repo

from src import tool_execution
from src.agent_worktree import repository_sync as rs
from src.github_credentials import github_git_host, token_for_git_host

TOKEN = "corp-enterprise-token-0123456789"


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """One configured root, empty; data and private dirs elsewhere."""
    development = tmp_path / "development"
    development.mkdir()
    monkeypatch.setattr(rs, "git_repository_roots", lambda: (development,))
    monkeypatch.setattr(rs, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(rs, "PERSONAL_DIR", str(tmp_path / "data" / "personal"))
    monkeypatch.setattr(rs, "vault_root", lambda: str(tmp_path / "vault"))
    return tmp_path


def _checkout(path):
    path.mkdir(parents=True)
    Repo.init(str(path)).close()
    return path


@contextmanager
def _workspace(path):
    token = tool_execution._active_workspace.set(str(path))
    try:
        yield
    finally:
        tool_execution._active_workspace.reset(token)


# ── workspace as a git root ────────────────────────────────────────────────────


def test_the_workspace_checkout_is_admitted(roots):
    project = _checkout(roots / "project")
    with pytest.raises(rs.RepositorySyncError) as excinfo:
        rs._validate_path(str(project))
    assert excinfo.value.code == "outside_roots"
    with _workspace(project):
        assert rs._validate_path(str(project)) == project.resolve()


def test_a_workspace_subfolder_admits_its_checkout_but_nothing_beside_it(roots):
    project = _checkout(roots / "project")
    (project / "src").mkdir()
    sibling = _checkout(roots / "sibling")
    nested = _checkout(project / "vendor" / "lib")
    with _workspace(project / "src"):
        assert rs.workspace_repository() == project.resolve()
        assert rs._validate_path(str(project)) == project.resolve()
        for other in (sibling, nested):
            with pytest.raises(rs.RepositorySyncError) as excinfo:
                rs._validate_path(str(other))
            assert excinfo.value.code == "outside_roots"
        with pytest.raises(rs.RepositorySyncError) as excinfo:
            rs._validate_path(str(project / "src"))
        assert excinfo.value.code == "unsupported_checkout"
        assert str(project.resolve()) in str(excinfo.value)


def test_a_workspace_that_is_not_a_checkout_is_still_refused(roots):
    plain = roots / "plain"
    plain.mkdir()
    with _workspace(plain):
        assert rs.workspace_repository() is None
        with pytest.raises(rs.RepositorySyncError) as excinfo:
            rs._validate_path(str(plain))
        assert excinfo.value.code == "unsupported_checkout"


def test_paths_escaping_the_workspace_are_refused(roots):
    project = _checkout(roots / "project")
    outside = _checkout(roots / "outside")
    (project / "link").symlink_to(outside, target_is_directory=True)
    with _workspace(project):
        with pytest.raises(rs.RepositorySyncError) as excinfo:
            rs._validate_path(str(project / "link"))
        assert excinfo.value.code == "symlink_path"
        with pytest.raises(rs.RepositorySyncError) as excinfo:
            rs._validate_path(str(project / ".." / "outside"))
        assert excinfo.value.code == "outside_roots"


def test_a_sensitive_or_root_workspace_is_never_admitted(roots, monkeypatch):
    project = _checkout(roots / "project")
    monkeypatch.setattr(tool_execution, "vet_workspace", lambda raw: None)
    with _workspace(project):
        assert rs.workspace_repository() is None
        assert project.resolve() not in rs._operating_roots()


def test_the_workspace_never_enters_the_cached_context_free_roots(roots):
    project = _checkout(roots / "project")
    with _workspace(project):
        assert project.resolve() not in rs.git_repository_roots()
        assert project.resolve() in rs._operating_roots()


@pytest.mark.asyncio
async def test_the_workspace_reaches_the_worker_thread_that_validates(roots):
    """Validation runs in asyncio.to_thread; the per-call workspace set by
    execute_tool_block has to arrive there, or a worker is refused again."""
    project = _checkout(roots / "project")
    with _workspace(project):
        seen = await rs.run_repository_operation(str(project), lambda path: path)
        listed = await rs.list_repositories()
    assert seen == project.resolve()
    assert [row["repository"] for row in listed] == [str(project.resolve())]


# ── Enterprise host: authenticated there, and only there ───────────────────────


@pytest.mark.parametrize(
    "value, expected",
    [
        ("", "github.com"),
        ("https://github.com", "github.com"),
        ("https://GHE.Example.com/", "ghe.example.com"),
        ("ghe.example.com", "ghe.example.com"),
        ("http://ghe.example.com", None),
        ("https://ghe.example.com:8443", None),
        ("https://user:pw@ghe.example.com", None),
        ("https://ghe.example.com/api/v3", None),
        ("https://ghe.example.com?x=1", None),
    ],
)
def test_the_git_host_follows_github_host(monkeypatch, value, expected):
    monkeypatch.setenv("GITHUB_HOST", value)
    assert github_git_host() == expected


def _capture_transport(monkeypatch):
    seen = []
    monkeypatch.setattr(
        rs,
        "get_transport_and_path_from_url",
        lambda url, **kwargs: seen.append((url, kwargs)) or (object(), "p"),
    )
    return seen


def test_enterprise_remote_gets_the_token_and_github_com_does_not(monkeypatch):
    monkeypatch.setenv("GITHUB_HOST", "https://ghe.example.com")
    seen = _capture_transport(monkeypatch)
    rs._transport(rs._https_url("https://ghe.example.com/acme/app.git"), TOKEN)
    rs._transport(rs._https_url("https://github.com/acme/app.git"), TOKEN)
    rs._transport("https://evil.example/acme/app.git", TOKEN)
    assert [kw["password"] for _url, kw in seen] == [TOKEN, None, None]
    assert [kw["username"] for _url, kw in seen] == ["x-access-token", None, None]
    assert all(TOKEN not in url for url, _kw in seen)


def test_public_host_token_never_reaches_an_enterprise_host(monkeypatch):
    monkeypatch.delenv("GITHUB_HOST", raising=False)
    seen = _capture_transport(monkeypatch)
    rs._transport("https://github.com/acme/app.git", "ghp_public")
    rs._transport("https://ghe.example.com/acme/app.git", "ghp_public")
    assert [kw["password"] for _url, kw in seen] == ["ghp_public", None]
    assert token_for_git_host("GitHub.com", "ghp_public") == "ghp_public"


def test_an_unusable_github_host_sends_the_token_nowhere(monkeypatch):
    monkeypatch.setenv("GITHUB_HOST", "http://ghe.example.com")
    assert token_for_git_host("ghe.example.com", TOKEN) is None
    assert token_for_git_host("github.com", TOKEN) is None


def test_enterprise_remotes_are_accepted_and_others_still_refused(monkeypatch):
    monkeypatch.setenv("GITHUB_HOST", "https://ghe.example.com")
    assert rs._https_url("git@ghe.example.com:acme/app.git") == (
        "https://ghe.example.com/acme/app.git"
    )
    assert rs._https_url("https://github.com/acme/app") == "https://github.com/acme/app"
    for url in (
        "https://gitlab.com/acme/app.git",
        "git@gitlab.com:acme/app.git",
        "https://x:y@ghe.example.com/acme/app.git",
        "http://ghe.example.com/acme/app.git",
    ):
        with pytest.raises(rs.RepositorySyncError) as excinfo:
            rs._https_url(url)
        assert excinfo.value.code == "unsupported_remote"
    monkeypatch.delenv("GITHUB_HOST")
    with pytest.raises(rs.RepositorySyncError):
        rs._https_url("https://ghe.example.com/acme/app.git")


# ── the token never comes back to the model ────────────────────────────────────


def test_the_remote_url_written_for_origin_carries_no_credential(monkeypatch):
    monkeypatch.setenv("GITHUB_HOST", "https://ghe.example.com")
    client, path = rs._transport("https://ghe.example.com/acme/app.git", TOKEN)
    try:
        # dulwich writes remote.origin.url from get_url() on clone.
        assert client.get_url(path) == "https://ghe.example.com/acme/app.git"
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def test_transport_errors_are_scrubbed_of_an_enterprise_token(monkeypatch):
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", TOKEN)
    basic = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    exc = RuntimeError(
        f"failed https://x-access-token:{TOKEN}@ghe.example.com/a/b "
        f"header Basic {basic} raw {TOKEN} timed out"
    )
    code, message = rs.describe_remote_failure(exc, operation="fetch", url="https://ghe.example.com/a/b")
    assert code == "network_error"
    assert TOKEN not in message and basic not in message
    assert "x-access-token:" not in message


def test_the_env_token_is_not_scrubbed_when_implausibly_short(monkeypatch):
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "a")
    assert rs._scrub_credential("a bad day") == "a bad day"
