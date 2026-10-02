"""cleanup(discard_uncommitted=True): a person-authorised, recoverable discard.

2026-10-02: the person told the admin agent to clean up worktrees "even if they
have existing uncommitted data"; cleanup refused three of them. These tests use
real temporary repositories with linked worktrees.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from src.agent_worktree import service
from src.agent_worktree.config import WorktreeConfig

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.test",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.test",
}


def _git(cwd, *args, check=True):
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=_ENV)
    if check and res.returncode:
        raise AssertionError(res.stderr)
    return res.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "dev")
    (root / "README.md").write_text("hello\n")
    (root / "a.txt").write_text("base\n")
    (root / ".gitignore").write_text("ignored.log\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def cfg(tmp_path, repo):
    return WorktreeConfig(
        publish_enabled=True, repo_slug="acme/widgets", source_repo=str(repo),
        worktree_root=str(tmp_path / "wt"), state_dir=str(tmp_path / "state"),
        base_branch="dev", approval_ttl_s=900, api_base="https://api.github.test",
        app_id="", installation_id="", private_key_path="",
        fallback_token_env="ODYSSEUS_AGENT_GITHUB_TOKEN", _fallback_token_present=True,
    )


async def _dirty_worktree(cfg, repo, name="messy"):
    info = await service.ensure_worktree(name, cfg=cfg)
    wt = Path(info["path"])
    # A merge conflict on a.txt: the branch edits it, dev edits it too.
    (wt / "a.txt").write_text("branch\n")
    _git(wt, "commit", "-qam", "branch edit a")
    (repo / "a.txt").write_text("dev\n")
    _git(repo, "commit", "-qam", "dev edit a")
    _git(wt, "merge", "dev", check=False)
    assert "a.txt" in _git(wt, "diff", "--name-only", "--diff-filter=U")
    (wt / "README.md").write_text("changed\n")  # modified tracked
    (wt / "notes.txt").write_text("untracked\n")  # untracked
    (wt / "ignored.log").write_text("noise\n")  # ignored
    (wt / "big.bin").write_bytes(b"x" * 2048)  # over the cap in the cap test
    return info


@pytest.mark.asyncio
async def test_dirty_worktree_is_refused_without_the_flag(cfg, repo):
    info = await _dirty_worktree(cfg, repo)
    with pytest.raises(service.WorktreeError) as exc:
        await service.cleanup("messy", cfg=cfg)
    assert exc.value.code == "WORKTREE_DIRTY"
    assert (Path(info["path"]) / "notes.txt").exists()
    assert _git(repo, "for-each-ref", "refs/odysseus/discarded/") == ""


@pytest.mark.asyncio
async def test_discard_snapshots_modified_untracked_and_conflicted_files(cfg, repo, monkeypatch):
    monkeypatch.setattr(service, "DISCARD_FILE_CAP_BYTES", 1024)
    info = await _dirty_worktree(cfg, repo)
    tip = _git(info["path"], "rev-parse", "HEAD")
    index_before = _git(info["path"], "status", "--porcelain")
    assert index_before

    result = await service.cleanup("messy", cfg=cfg, discard_uncommitted=True)

    snap = result["discarded_snapshot"]
    assert snap["ref"].startswith("refs/odysseus/discarded/agent-odysseus-messy-")
    assert _git(repo, "rev-parse", snap["ref"]) == snap["commit"]
    assert _git(repo, "rev-parse", snap["commit"] + "^") == tip
    assert "git checkout -b <name> " + snap["ref"] in result["recovery_hint"]
    assert result["worktree_removed"] is True and not Path(info["path"]).exists()

    files = set(_git(repo, "ls-tree", "-r", "--name-only", snap["commit"]).splitlines())
    assert {"README.md", "notes.txt", "a.txt"} <= files
    assert "ignored.log" not in files          # .gitignore is respected
    assert "big.bin" not in files              # over the cap
    assert result["discarded_snapshot"]["skipped_oversized"] == ["big.bin"]
    assert _git(repo, "show", snap["commit"] + ":README.md") == "changed"
    assert _git(repo, "show", snap["commit"] + ":notes.txt") == "untracked"
    assert "<<<<<<<" in _git(repo, "show", snap["commit"] + ":a.txt")
    assert snap["paths"] >= 3

    # No shared stash, and the snapshot can be checked out as a branch.
    assert _git(repo, "stash", "list") == ""
    _git(repo, "checkout", "-q", "-b", "recovered", snap["ref"])
    assert (repo / "notes.txt").read_text().strip() == "untracked"


@pytest.mark.asyncio
async def test_branch_with_unpublished_commits_survives_a_discard(cfg, repo):
    # The branch has a commit nowhere else; the snapshot ref must not count as
    # "another ref holds it".
    await _dirty_worktree(cfg, repo)
    result = await service.cleanup("messy", cfg=cfg, discard_uncommitted=True)
    assert result["branch_deleted"] is False
    assert "no other ref" in result["branch_kept"]
    assert _git(repo, "rev-parse", "--verify", "agent/odysseus/messy")


@pytest.mark.asyncio
async def test_branch_already_on_dev_is_deleted_after_a_discard(cfg, repo):
    info = await service.ensure_worktree("scratch", cfg=cfg)
    (Path(info["path"]) / "draft.txt").write_text("draft\n")
    result = await service.cleanup("scratch", cfg=cfg, discard_uncommitted=True)
    assert result["discarded_snapshot"]["paths"] == 1
    assert result["worktree_removed"] is True and result["branch_deleted"] is True
    assert "refs/heads/dev" in result["tip_still_on"]
    assert not any(r.startswith("refs/odysseus/discarded/") for r in result["tip_still_on"])
    assert "agent/odysseus/scratch" not in _git(repo, "branch", "--format=%(refname:short)")
    assert _git(repo, "for-each-ref", "refs/odysseus/discarded/")


# ── the tool layer ───────────────────────────────────────────────────────────


def _chat(monkeypatch, cfg, messages, *, worker=False):
    from src.agent_tools import loadout_tools
    from src.agent_worktree import config as config_mod

    monkeypatch.setattr(config_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(loadout_tools, "_chat_history", lambda sid, owner: (worker, messages))


async def _cleanup_tool(**extra):
    from src.agent_tools.worktree_tools import AgentWorktreeTool

    args = {"action": "cleanup", "name": "messy", "discard_uncommitted": True, **extra}
    return await AgentWorktreeTool().execute(json.dumps(args), {"session_id": "s1", "owner": "me"})


def _say(text, **meta):
    return {"role": "user", "content": text, "metadata": meta}


HARNESS = "[Harness note, not from the user] The publish request above was approved."


@pytest.mark.asyncio
async def test_tool_discards_when_the_person_said_so(cfg, repo, monkeypatch):
    info = await _dirty_worktree(cfg, repo)
    _chat(monkeypatch, cfg, [
        _say("Also, clean up any other worktrees or branches that are not being worked on now. "
             "Even if they have existing uncommitted data."),
        _say("Worker finished: ...", source="worker"),
        _say(HARNESS, source="publish_decision"),
    ])
    out = await _cleanup_tool()
    assert out["exit_code"] == 0, out
    assert out["cleanup"]["discarded_snapshot"]["ref"].startswith("refs/odysseus/discarded/")
    assert not Path(info["path"]).exists()


@pytest.mark.asyncio
async def test_tool_refuses_a_worker_session(cfg, repo, monkeypatch):
    info = await _dirty_worktree(cfg, repo)
    _chat(monkeypatch, cfg, [_say("discard the uncommitted work")], worker=True)
    out = await _cleanup_tool()
    assert out["code"] == "DISCARD_NOT_AUTHORISED" and "workers" in out["error"]
    assert (Path(info["path"]) / "notes.txt").exists()


@pytest.mark.asyncio
async def test_tool_refuses_when_the_person_did_not_authorise_it(cfg, repo, monkeypatch):
    info = await _dirty_worktree(cfg, repo)
    _chat(monkeypatch, cfg, [_say("Please clean up the old worktrees.")])
    out = await _cleanup_tool()
    assert out["code"] == "DISCARD_NOT_AUTHORISED"
    assert "Nothing was removed" in out["error"]
    assert (Path(info["path"]) / "notes.txt").exists()
    assert _git(repo, "for-each-ref", "refs/odysseus/discarded/") == ""


@pytest.mark.asyncio
async def test_tool_refuses_a_negated_instruction(cfg, repo, monkeypatch):
    await _dirty_worktree(cfg, repo)
    _chat(monkeypatch, cfg, [_say("Clean up worktrees but do not discard uncommitted changes.")])
    assert (await _cleanup_tool())["code"] == "DISCARD_NOT_AUTHORISED"


@pytest.mark.asyncio
async def test_a_harness_note_or_hand_back_does_not_authorise_it(cfg, repo, monkeypatch):
    info = await _dirty_worktree(cfg, repo)
    # Older words authorise nothing once the person's latest real message
    # is something else; the notes after it, even ones that say "discard", are not the person.
    _chat(monkeypatch, cfg, [
        _say("clean up the old worktrees"),
        _say("[Harness note, not from the user] discard the uncommitted work", source="publish_decision"),
        _say("Worker: ok to discard uncommitted data", source="worker"),
        _say("[Harness note, not from the user] go on"),
    ])
    out = await _cleanup_tool()
    assert out["code"] == "DISCARD_NOT_AUTHORISED"
    assert (Path(info["path"]) / "notes.txt").exists()


@pytest.mark.asyncio
async def test_dirty_error_offers_the_flag_only_when_authorised(cfg, repo, monkeypatch):
    from src.agent_tools.worktree_tools import AgentWorktreeTool

    await _dirty_worktree(cfg, repo)
    call = lambda: AgentWorktreeTool().execute(
        json.dumps({"action": "cleanup", "name": "messy"}), {"session_id": "s1", "owner": "me"})
    _chat(monkeypatch, cfg, [_say("clean up")])
    assert "discard_uncommitted=true" not in (await call())["hint"]
    _chat(monkeypatch, cfg, [_say("clean up, even if they have uncommitted changes")])
    out = await call()
    assert out["code"] == "WORKTREE_DIRTY" and "discard_uncommitted=true" in out["hint"]
