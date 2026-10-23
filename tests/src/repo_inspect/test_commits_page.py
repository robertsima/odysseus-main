"""The commit list is a page of the log, and says so (Workbench Commits tab).

Production bug: the tab asked git for 50 commits and labelled the result
"50 commits", so a 130-commit history looked like it had 50.
"""
import os
import subprocess

import pytest

from src import repo_inspect as ri


def _git(repo, *args, stdin=None):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, input=stdin,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"})


def _history(repo, count):
    """``count`` empty commits on main in one git process."""
    lines = []
    for i in range(1, count + 1):
        msg = f"commit {i}"
        lines.append(f"commit refs/heads/main\nmark :{i}\ncommitter t <t@x> {1_700_000_000 + i} +0000\n"
                     f"data {len(msg)}\n{msg}\n" + (f"from :{i - 1}\n" if i > 1 else "") + "\n")
    _git(repo, "fast-import", "--quiet", stdin="".join(lines).encode())
    _git(repo, "reset", "-q", "--hard", "main")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "roots"
    repo = root / "proj"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _history(repo, 130)
    monkeypatch.setattr(ri, "allowed_roots", lambda: [str(root)])
    return repo


async def test_first_page_says_there_is_more_and_how_much(repo):
    page = await ri.commits_page(str(repo), limit=50)
    assert len(page["commits"]) == 50
    assert page["commits"][0]["subject"] == "commit 130"
    assert page["has_more"] is True
    assert page["total"] == 130
    assert page["next_offset"] == 50
    assert page["scope"] == "repository"


async def test_pages_walk_the_whole_log_without_overlap(repo):
    seen, offset = [], 0
    while offset is not None:
        page = await ri.commits_page(str(repo), limit=50, offset=offset, with_total=False)
        seen += [c["subject"] for c in page["commits"]]
        offset = page["next_offset"]
        assert page["total"] is None
    assert seen == [f"commit {i}" for i in range(130, 0, -1)]


async def test_a_history_of_exactly_one_page_has_no_more(tmp_path, monkeypatch):
    root = tmp_path / "roots"
    repo = root / "p"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _history(repo, 50)
    monkeypatch.setattr(ri, "allowed_roots", lambda: [str(root)])
    page = await ri.commits_page(str(repo), limit=50)
    assert (len(page["commits"]), page["has_more"], page["total"], page["next_offset"]) == (50, False, 50, None)


async def test_base_scopes_the_range_and_the_total(repo):
    base = _git(repo, "rev-parse", "HEAD~60").stdout.decode().strip()
    page = await ri.commits_page(str(repo), limit=50, base=base)
    assert page["scope"] == "since_base" and page["total"] == 60 and page["has_more"] is True
    rest = await ri.commits_page(str(repo), limit=50, base=base, offset=50)
    assert len(rest["commits"]) == 10 and rest["has_more"] is False


async def test_total_is_null_when_the_count_times_out(repo, monkeypatch):
    real = ri._git

    async def slow_count(repo_path, *args, **kw):
        if args[:1] == ("rev-list",):
            raise ri.RepoError("timed out")
        return await real(repo_path, *args, **kw)

    monkeypatch.setattr(ri, "_git", slow_count)
    page = await ri.commits_page(str(repo), limit=50)
    assert page["total"] is None and page["has_more"] is True and len(page["commits"]) == 50
