"""Read-only git inspection behind the Workbench Changes/Commits views."""
import os
import subprocess

import pytest

from src import repo_inspect as ri


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"})


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "roots"
    repo = root / "proj"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("print('one')\nprint('two')\n", encoding="utf-8")
    (repo / "keep.txt").write_text("k\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "first")
    monkeypatch.setattr(ri, "allowed_roots", lambda: [str(root)])
    return repo


async def test_confinement_refuses_paths_outside_the_roots(repo, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    _git(outside, "init", "-q")
    with pytest.raises(ri.RepoError, match="outside"):
        ri.resolve_repo(str(outside))
    with pytest.raises(ri.RepoError, match="not a git checkout"):
        ri.resolve_repo(str(repo.parent))
    assert ri.resolve_repo(str(repo)) == os.path.realpath(str(repo))
    # An explicit root list lets an already-approved checkout skip the lookup.
    assert ri.resolve_repo(str(outside), roots=[str(outside)]) == os.path.realpath(str(outside))


def test_file_and_ref_validation(repo):
    r = str(repo)
    assert ri.relative_file(r, "sub/../a.py") == "a.py"
    for bad in ("/etc/passwd", "../x", "..", "", "-rf"):
        with pytest.raises(ri.RepoError):
            ri.relative_file(r, bad)
    assert ri.validate_ref("HEAD~1") == "HEAD~1"
    assert ri.validate_ref(None) == "HEAD"
    for bad in ("-", "--output=x", "a b", "a..b"):
        with pytest.raises(ri.RepoError):
            ri.validate_ref(bad)


async def test_status_changes_and_diff_reflect_the_working_tree(repo):
    (repo / "a.py").write_text("print('one')\nprint('three')\nprint('four')\n", encoding="utf-8")
    (repo / "new.md").write_text("# hi\nline\n", encoding="utf-8")
    (repo / "keep.txt").unlink()

    st = await ri.status(str(repo))
    assert st["branch"] == "main" and st["dirty"] and st["untracked_count"] == 1

    ch = await ri.changes(str(repo))
    by_path = {f["path"]: f for f in ch["files"]}
    assert by_path["a.py"]["status"] == "modified" and by_path["a.py"]["additions"] == 2 and by_path["a.py"]["deletions"] == 1
    assert by_path["keep.txt"]["status"] == "deleted"
    assert by_path["new.md"]["status"] == "untracked" and by_path["new.md"]["additions"] == 2
    assert ch["total_additions"] == 4 and ch["total_deletions"] == 2

    diff = await ri.file_diff(str(repo), "a.py")
    assert "-print('two')" in diff["diff"] and "+print('three')" in diff["diff"]
    untracked = await ri.file_diff(str(repo), "new.md")
    assert "+# hi" in untracked["diff"], "untracked files diff as a whole-file addition"

    old = await ri.file_at(str(repo), "a.py", ref="HEAD")
    new = await ri.file_at(str(repo), "a.py", ref="worktree")
    assert "two" in old["content"] and "four" in new["content"]
    assert (await ri.file_at(str(repo), "new.md", ref="HEAD"))["missing"] is True


async def test_commits_and_commit_detail_and_base_scoping(repo):
    start = (await ri.status(str(repo)))["head"]
    (repo / "a.py").write_text("print('changed')\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "second: change a")
    (repo / "b.py").write_text("x = 1\n", encoding="utf-8")

    log = await ri.commits(str(repo), limit=10)
    assert [c["subject"] for c in log] == ["second: change a", "first"]
    since = await ri.commits(str(repo), base=start)
    assert [c["subject"] for c in since] == ["second: change a"]

    detail = await ri.commit_detail(str(repo), since[0]["sha"])
    assert detail["subject"] == "second: change a"
    assert detail["files"][0]["path"] == "a.py" and "+print('changed')" in detail["patch"]

    # Relative to the run's start commit: the commit AND the untracked file show up.
    ch = await ri.changes(str(repo), base=start)
    assert {f["path"] for f in ch["files"]} == {"a.py", "b.py"}


async def test_diff_output_is_capped(repo, monkeypatch):
    monkeypatch.setattr(ri, "MAX_DIFF_CHARS", 200)
    (repo / "a.py").write_text("\n".join(f"line {i}" for i in range(400)), encoding="utf-8")
    diff = await ri.file_diff(str(repo), "a.py")
    assert diff["truncated"] is True and len(diff["diff"]) < 300
