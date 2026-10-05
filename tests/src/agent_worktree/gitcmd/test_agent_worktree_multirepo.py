"""manage_agent_worktree for repositories other than the configured source.

Replays the 2026-09-28 failure: a worker asked for an isolated worktree of the
Umni checkout based exactly on origin/main and called
``{"action": "start", "name": "umni-checkin-main", "branch": "origin/main"}``.
The tool was bound to the Odysseus checkout, so it created
``agent/odysseus/origin/main`` there — the wrong repository, with a branch
named after the base ref. These tests run against real temporary repositories
(a bare "remote", a clone with origin/main, the operator's source checkout);
only the network push and the GitHub API are substituted.
"""

import dataclasses
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from src.agent_worktree import approval as approval_mod
from src.agent_worktree import repository_sync as rs
from src.agent_worktree import service
from src.agent_worktree.config import WorktreeConfig
from src.agent_worktree.gitcmd import GitResult, run_git as real_run_git

pytestmark = [
    pytest.mark.security,
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
]

UMNI_URL = "https://github.com/robertsima/Umni.git"


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.test",
             "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.test"},
    ).stdout.strip()


@pytest.fixture
def world(tmp_path, monkeypatch):
    # The operator's own checkout (Odysseus), outside the repository roots.
    source = tmp_path / "odysseus"
    source.mkdir()
    _git(source, "init", "-q", "-b", "dev")
    _git(source, "config", "core.autocrlf", "false")
    (source / "README.md").write_text("odysseus\n")
    _git(source, "add", "-A")
    _git(source, "commit", "-q", "-m", "init")

    # A fake GitHub remote for Umni, and the user's checkout of it.
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "app.py").write_text("print('umni')\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "main")
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(remote))

    roots = tmp_path / "development"
    roots.mkdir()
    umni = roots / "dog-trainer"
    _git(roots, "clone", "-q", str(remote), str(umni))
    _git(umni, "config", "core.autocrlf", "false")
    # The user's own feature branch, which must never be touched.
    _git(umni, "checkout", "-q", "-b", "feature/dog-trainer-next")
    (umni / "feature.py").write_text("wip = 1\n")
    _git(umni, "add", "-A")
    _git(umni, "commit", "-q", "-m", "feature work")
    # The clone's origin points at the local bare repo; publishing derives its
    # target from a github.com origin, so present it as one.
    _git(umni, "remote", "set-url", "origin", UMNI_URL)

    monkeypatch.setattr(rs, "repository_roots", lambda: (roots,))
    monkeypatch.setattr(rs, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(rs, "PERSONAL_DIR", str(tmp_path / "data" / "personal_docs"))
    monkeypatch.setenv("ODYSSEUS_AGENT_SOURCE_REPO", str(source))
    monkeypatch.setenv("ODYSSEUS_AGENT_WORKTREE_ROOT", str(tmp_path / "wt"))
    monkeypatch.setenv("ODYSSEUS_AGENT_STATE_DIR", str(tmp_path / "state"))

    cfg = WorktreeConfig(
        publish_enabled=True,
        repo_slug="acme/odysseus",
        source_repo=str(source),
        worktree_root=str(tmp_path / "wt"),
        state_dir=str(tmp_path / "state"),
        base_branch="dev",
        approval_ttl_s=900,
        api_base="https://api.github.test",
        app_id="",
        installation_id="",
        private_key_path="",
        fallback_token_env="ODYSSEUS_AGENT_GITHUB_TOKEN",
        _fallback_token_present=True,
    )
    origin_main = _git(umni, "rev-parse", "origin/main")
    return {"cfg": cfg, "source": source, "umni": umni, "origin_main": origin_main,
            "tmp": tmp_path}


def _branches(repo) -> set:
    return set(_git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())


async def test_start_in_another_repository_from_origin_main(world):
    cfg, umni = world["cfg"], world["umni"]
    info = await service.ensure_worktree(
        "checkin", cfg=cfg, repository=str(umni), base="origin/main",
        expected_base=world["origin_main"],
    )
    assert info["branch"] == "agent/umni/checkin"  # named after the GitHub repository
    assert Path(info["repository"]) == umni.resolve()
    assert info["repo"] == "robertsima/Umni"
    assert info["base"] == "origin/main" and info["base_sha"] == world["origin_main"]
    assert info["pr_base"] == "main"
    path = Path(info["path"])
    # Namespaced per repository, so equal task names in two repos never collide.
    assert path.parent.parent == Path(cfg.worktree_root).resolve() / "_repos"
    assert _git(path, "rev-parse", "HEAD") == world["origin_main"]
    # The branch starts at the commit and tracks nothing (no accidental pull/push to main).
    assert subprocess.run(["git", "config", "--get", "branch.agent/umni/checkin.remote"],
                          cwd=str(umni), capture_output=True).returncode != 0
    # The user's checkout is untouched; the Odysseus checkout gained nothing.
    assert _git(umni, "rev-parse", "--abbrev-ref", "HEAD") == "feature/dog-trainer-next"
    assert _git(umni, "status", "--porcelain") == ""
    assert _branches(world["source"]) == {"dev"}


async def test_the_same_name_in_two_repositories_gets_two_paths(world):
    cfg = world["cfg"]
    ours = await service.ensure_worktree("task", cfg=cfg)
    theirs = await service.ensure_worktree("task", cfg=cfg, repository=str(world["umni"]),
                                           base="origin/main")
    assert ours["branch"] == "agent/odysseus/task"
    assert theirs["branch"] == "agent/umni/task"
    assert ours["path"] != theirs["path"]
    # Legacy layout for the configured source repository is unchanged.
    assert Path(ours["path"]) == Path(cfg.worktree_root).resolve() / "task"
    listing = await service.status(cfg=cfg)
    owners = {Path(w["path"]).name + "@" + Path(w["repository"]).name for w in listing["worktrees"]}
    assert owners == {"task@odysseus", "task@dog-trainer"}


async def test_a_remote_ref_given_as_branch_is_used_as_the_base(world):
    """The exact production call, plus the repository it lacked."""
    info = await service.ensure_worktree(
        "umni-checkin-main", branch="origin/main", cfg=world["cfg"],
        repository=str(world["umni"]),
    )
    assert info["branch"] == "agent/umni/umni-checkin-main"
    assert info["base"] == "origin/main"
    assert any("used as the base" in note for note in info["notes"])
    assert not any("origin" in b for b in _branches(world["umni"]) - {"feature/dog-trainer-next"})


async def test_a_branch_named_after_a_remote_ref_is_never_created(world):
    cfg, umni = world["cfg"], world["umni"]
    with pytest.raises(service.WorktreeError) as exc:
        await service.ensure_worktree("origin/main", cfg=cfg, repository=str(umni))
    assert exc.value.code == "MISSING_BRANCH"
    with pytest.raises(service.WorktreeError) as exc:
        await service.ensure_worktree("agent/umni/origin/main", cfg=cfg, repository=str(umni))
    assert exc.value.code == "BRANCH_IS_BASE"
    with pytest.raises(service.WorktreeError) as exc:
        await service.ensure_worktree("x", branch="origin/main", base="origin/other",
                                      cfg=cfg, repository=str(umni))
    assert exc.value.code == "BRANCH_IS_BASE"
    # Also in the configured source repository once it has an origin.
    _git(world["source"], "remote", "add", "origin", "https://github.com/acme/odysseus.git")
    with pytest.raises(service.WorktreeError):
        await service.ensure_worktree("origin/main", cfg=cfg)
    assert _branches(umni) == {"main", "feature/dog-trainer-next"}
    assert _branches(world["source"]) == {"dev"}


async def test_expected_base_mismatch_creates_nothing(world):
    cfg, umni = world["cfg"], world["umni"]
    with pytest.raises(service.WorktreeError) as exc:
        await service.ensure_worktree("checkin", cfg=cfg, repository=str(umni),
                                      base="origin/main", expected_base="0" * 40)
    assert exc.value.code == "BASE_MISMATCH"
    assert world["origin_main"] in str(exc.value)
    assert "agent/umni/checkin" not in _branches(umni)
    assert not (Path(cfg.worktree_root) / "_repos").exists() or not any(
        (Path(cfg.worktree_root) / "_repos").rglob("checkin"))
    with pytest.raises(service.WorktreeError) as exc:
        await service.ensure_worktree("checkin", cfg=cfg, repository=str(umni),
                                      base="origin/main", expected_base="abc123")
    assert exc.value.code == "INVALID_BASE"


async def test_reuse_refuses_a_different_base(world):
    cfg, umni = world["cfg"], world["umni"]
    await service.ensure_worktree("checkin", cfg=cfg, repository=str(umni), base="origin/main")
    again = await service.ensure_worktree("checkin", cfg=cfg, repository=str(umni),
                                          base="origin/main")
    assert again["exists"] is True
    with pytest.raises(service.WorktreeError) as exc:
        await service.ensure_worktree("checkin", cfg=cfg, repository=str(umni),
                                      base="feature/dog-trainer-next")
    assert exc.value.code == "BASE_MISMATCH"


async def test_a_linked_worktree_path_resolves_to_its_repository(world):
    cfg, umni = world["cfg"], world["umni"]
    audit = umni.parent / "umni-main-audit"
    _git(umni, "worktree", "add", "-q", "-b", "audit", str(audit), "origin/main")
    info = await service.ensure_worktree("checkin", cfg=cfg, repository=str(audit),
                                         base="origin/main")
    assert Path(info["repository"]) == umni.resolve()


async def test_a_repository_outside_the_roots_is_refused(world):
    stray = world["tmp"] / "elsewhere"
    stray.mkdir()
    _git(stray, "init", "-q")
    with pytest.raises(service.WorktreeError) as exc:
        await service.ensure_worktree("x", cfg=world["cfg"], repository=str(stray), base="HEAD")
    assert exc.value.code == "INVALID_REPOSITORY"


async def test_commit_and_diff_find_the_worktree_by_name(world):
    cfg, umni = world["cfg"], world["umni"]
    info = await service.ensure_worktree("checkin", cfg=cfg, repository=str(umni),
                                         base="origin/main")
    (Path(info["path"]) / "checkin.py").write_text("ok = True\n")
    # No repository needed on later calls: the name is unique across repos.
    result = await service.commit("checkin", "add checkin", cfg=cfg)
    assert result["committed"] is True
    assert _git(umni, "rev-parse", "agent/umni/checkin") == result["head_sha"]
    summary = await service.diff_summary("checkin", cfg=cfg)
    assert summary["changed_files"] == ["checkin.py"]
    assert summary["base"] == "origin/main"
    assert Path(summary["repository"]) == umni.resolve()


def _fake_remote_head(value):
    async def _inner(cfg, branch, token):
        return value
    return _inner


async def test_publish_pushes_to_that_repositorys_remote(world, monkeypatch):
    cfg, umni = world["cfg"], world["umni"]
    info = await service.ensure_worktree("checkin", cfg=cfg, repository=str(umni),
                                         base="origin/main")
    (Path(info["path"]) / "checkin.py").write_text("ok = True\n")
    await service.commit("checkin", "add checkin", cfg=cfg, repository=str(umni))

    async def _token(_cfg):
        return "ghs_" + "t" * 36
    monkeypatch.setattr("src.agent_worktree.github.resolve_token", _token)
    monkeypatch.setattr(service, "_remote_head", _fake_remote_head(None))

    view = await service.request_publish("checkin", title="Check-in", cfg=cfg,
                                         repository=str(umni))
    assert view["repo"] == "robertsima/Umni"
    assert Path(view["repository"]) == umni.resolve()
    assert view["base_branch"] == "main"
    assert view["changed_files"] == ["checkin.py"]

    # Still behind the human approval code.
    with pytest.raises(approval_mod.ApprovalError):
        await service.publish(view["id"], "guessed", cfg=cfg)
    code, _ = approval_mod.grant(view["id"], granted_by="alice", cfg=cfg)

    pushes, prs = [], {}

    async def watch(args, **kwargs):
        if list(args)[:1] == ["push"]:
            pushes.append({"args": list(args), "cwd": kwargs.get("cwd"),
                           "env": kwargs.get("extra_env") or {}})
            return GitResult(code=0, stdout="", stderr="")
        return await real_run_git(args, **kwargs)

    async def create_pr(pr_cfg, _token, **kwargs):
        prs.update(kwargs, slug=pr_cfg.repo_slug)
        return {"number": 3, "url": "u", "draft": True, "state": "open"}

    async def find_pr(_cfg, _token, _branch):
        return None

    monkeypatch.setattr(service, "run_git", watch)
    monkeypatch.setattr("src.agent_worktree.github.create_draft_pr", create_pr)
    monkeypatch.setattr("src.agent_worktree.github.find_open_pr", find_pr)

    result = await service.publish(view["id"], code, cfg=cfg)
    assert result["repo"] == "robertsima/Umni"
    assert len(pushes) == 1
    push = pushes[0]
    assert UMNI_URL in push["args"]
    assert "https://github.com/acme/odysseus.git" not in push["args"]
    assert f"{view['head_sha']}:refs/heads/agent/umni/checkin" in push["args"]
    assert "--no-verify" in push["args"]
    assert Path(push["cwd"]) == umni.resolve()
    pairs = {push["env"][f"GIT_CONFIG_KEY_{i}"]: push["env"][f"GIT_CONFIG_VALUE_{i}"]
             for i in range(int(push["env"]["GIT_CONFIG_COUNT"]))}
    assert pairs[f"http.{UMNI_URL}.extraheader"].startswith("Authorization: Basic ")
    assert pairs["credential.helper"] == "" and pairs["protocol.allow"] == "never"
    assert pairs["core.hooksPath"] and pairs["core.fsmonitor"] == "false"
    assert prs["slug"] == "robertsima/Umni" and prs["base_branch"] == "main"
    assert prs["head_branch"] == "agent/umni/checkin"


async def test_publish_refuses_repository_config_that_could_redirect_the_push(world, monkeypatch):
    cfg, umni = world["cfg"], world["umni"]
    info = await service.ensure_worktree("checkin", cfg=cfg, repository=str(umni),
                                         base="origin/main")
    (Path(info["path"]) / "checkin.py").write_text("ok = True\n")
    await service.commit("checkin", "add checkin", cfg=cfg, repository=str(umni))
    _git(umni, "config", "url.https://evil.example/.insteadOf", "https://github.com/")
    with pytest.raises(service.WorktreeError, match=r"publishing is not available.*\[url\]"):
        await service.request_publish("checkin", title="x", cfg=cfg, repository=str(umni))


async def test_a_repository_without_a_github_origin_cannot_publish(world):
    cfg, umni = world["cfg"], world["umni"]
    _git(umni, "remote", "set-url", "origin", "https://gitlab.com/acme/umni.git")
    info = await service.ensure_worktree("checkin", cfg=cfg, repository=str(umni),
                                         base="origin/main")
    assert info["repo"] is None
    assert info["branch"] == "agent/dog-trainer/checkin"
    (Path(info["path"]) / "checkin.py").write_text("ok = True\n")
    await service.commit("checkin", "add", cfg=cfg, repository=str(umni))
    with pytest.raises(service.WorktreeError, match="no https://github.com origin remote"):
        await service.request_publish("checkin", title="x", cfg=cfg, repository=str(umni))


# ── cleanup ──────────────────────────────────────────────────────────────────


async def test_cleanup_removes_the_stray_origin_main_worktree(world):
    """The server's leftover: agent/odysseus/origin/main at <root>/origin__main."""
    cfg, source = world["cfg"], world["source"]
    stray = Path(cfg.worktree_root) / "origin__main"
    stray.parent.mkdir(parents=True, exist_ok=True)
    _git(source, "worktree", "add", "-q", "-b", "agent/odysseus/origin/main", str(stray), "dev")

    listing = await service.status(cfg=cfg)
    assert any(w["branch"] == "agent/odysseus/origin/main" for w in listing["worktrees"])

    result = await service.cleanup("agent/odysseus/origin/main", cfg=cfg)
    assert result["worktree_removed"] is True and result["branch_deleted"] is True
    assert "refs/heads/dev" in result["tip_still_on"]
    assert not stray.exists()
    assert _branches(source) == {"dev"}


async def test_cleanup_refuses_a_dirty_worktree(world):
    cfg, umni = world["cfg"], world["umni"]
    info = await service.ensure_worktree("checkin", cfg=cfg, repository=str(umni),
                                         base="origin/main")
    (Path(info["path"]) / "draft.txt").write_text("unsaved\n")
    with pytest.raises(service.WorktreeError) as exc:
        await service.cleanup("checkin", cfg=cfg, repository=str(umni))
    assert exc.value.code == "WORKTREE_DIRTY"
    assert (Path(info["path"]) / "draft.txt").exists()
    assert "agent/umni/checkin" in _branches(umni)


async def test_cleanup_keeps_a_branch_with_unpublished_commits(world):
    cfg, umni = world["cfg"], world["umni"]
    info = await service.ensure_worktree("checkin", cfg=cfg, repository=str(umni),
                                         base="origin/main")
    (Path(info["path"]) / "checkin.py").write_text("ok = True\n")
    committed = await service.commit("checkin", "work", cfg=cfg, repository=str(umni))
    result = await service.cleanup("checkin", cfg=cfg)
    assert result["worktree_removed"] is True
    assert result["branch_deleted"] is False and "no other ref" in result["branch_kept"]
    assert _git(umni, "rev-parse", "agent/umni/checkin") == committed["head_sha"]


# ── the agent-facing tool ────────────────────────────────────────────────────


async def _tool(payload):
    from src.agent_tools.worktree_tools import AgentWorktreeTool

    return await AgentWorktreeTool().execute(json.dumps(payload), {})


async def test_tool_start_with_repository_and_base(world):
    result = await _tool({"action": "start", "name": "umni-checkin-main", "branch": "origin/main",
                          "repository": str(world["umni"]),
                          "expected_base": world["origin_main"]})
    assert result["exit_code"] == 0, result
    assert result["worktree"]["branch"] == "agent/umni/umni-checkin-main"
    assert result["worktree"]["base_sha"] == world["origin_main"]


async def test_tool_refuses_a_repositoryless_start_from_another_projects_workspace(
    world, monkeypatch
):
    monkeypatch.setattr(rs, "workspace_repository", lambda: world["umni"].resolve())
    result = await _tool({"action": "start", "name": "umni-checkin-main", "branch": "origin/main"})
    assert result["exit_code"] == 1
    assert result["code"] == "REPOSITORY_REQUIRED"
    assert result["next_action"]["repository"] == str(world["umni"].resolve())
    assert _branches(world["source"]) == {"dev"}


async def test_tool_reads_a_worktree_path_given_as_repository_as_that_worktree(world):
    # 2026-09-29: a worker passed the worktree it stood in as `repository` and
    # `diff` answered "'name' is required".
    started = await _tool({"action": "start", "name": "fix-ci", "repository": str(world["umni"]),
                           "base": "origin/main"})
    assert started["exit_code"] == 0, started
    path = started["worktree"]["path"]
    (Path(path) / "fix.py").write_text("fixed = True\n")
    committed = await _tool({"action": "commit", "repository": path, "message": "fix ci"})
    assert committed["exit_code"] == 0, committed
    assert committed["result"]["committed"] is True
    diff = await _tool({"action": "diff", "repository": path})
    assert diff["exit_code"] == 0, diff
    assert diff["diff"]["changed_files"] == ["fix.py"]


async def test_tool_cleanup_action_and_remove_stays_forbidden(world):
    started = await _tool({"action": "start", "name": "tmp", "repository": str(world["umni"]),
                           "base": "origin/main"})
    assert started["exit_code"] == 0, started
    removed = await _tool({"action": "remove", "name": "tmp"})
    assert removed["code"] == "forbidden_by_policy" and "cleanup" in removed["error"]
    cleaned = await _tool({"action": "cleanup", "name": "tmp"})
    assert cleaned["exit_code"] == 0, cleaned
    assert cleaned["cleanup"]["worktree_removed"] is True
    assert cleaned["cleanup"]["branch_deleted"] is True


async def test_tool_base_mismatch_gives_the_fix(world):
    result = await _tool({"action": "start", "name": "c", "repository": str(world["umni"]),
                          "base": "origin/main", "expected_base": "1" * 40})
    assert result["exit_code"] == 1 and result["code"] == "BASE_MISMATCH"
    assert result["next_action"]["repository"] == str(world["umni"])


def test_existing_config_callers_keep_the_default_namespace(world):
    cfg = world["cfg"]
    assert cfg.branch_prefix == "agent/odysseus/" and cfg.repository_key == ""
    assert dataclasses.replace(cfg, repo_slug="x/y").branch_prefix == "agent/odysseus/"


async def test_a_rewritten_worktree_pointer_is_not_followed(world):
    """The agent can write its worktree's `.git` file; git must not follow it."""
    cfg = world["cfg"]
    info = await service.ensure_worktree("task", cfg=cfg)
    path = Path(info["path"])
    evil = path / "evil"
    evil.mkdir()
    _git(evil, "init", "-q")
    (path / ".git").unlink()  # Git for Windows marks it hidden; replace, not truncate
    (path / ".git").write_text(f"gitdir: {(evil / '.git').as_posix()}\n")
    with pytest.raises(service.WorktreeError, match="metadata check"):
        await service.commit("task", "x", cfg=cfg)
    listing = await service.status(cfg=cfg)
    assert [w["error"] for w in listing["worktrees"] if w["path"] == str(path)]


async def test_repository_hooks_do_not_run(world):
    cfg, umni = world["cfg"], world["umni"]
    marker = world["tmp"] / "hook-ran"
    hook = umni / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n", newline="\n")
    os.chmod(hook, 0o755)
    info = await service.ensure_worktree("checkin", cfg=cfg, repository=str(umni),
                                         base="origin/main")
    (Path(info["path"]) / "a.py").write_text("a = 1\n")
    await service.commit("checkin", "a", cfg=cfg, repository=str(umni))
    assert not marker.exists()
