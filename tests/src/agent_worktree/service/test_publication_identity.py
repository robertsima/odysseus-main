"""Catch publishing a registered branch instead of the commit tested in a clone."""
from pathlib import Path

import pytest

from src.agent_worktree import service
from tests.src.agent_worktree.gitcmd.test_agent_worktree_service import (
    _git, _prepare_change, cfg, repo, git_required,
)

pytestmark = [pytest.mark.security, git_required]


async def test_publish_refuses_tested_commit_from_separate_clone_before_credentials(cfg, tmp_path):
    path = await _prepare_change(cfg)
    clone = tmp_path / "review-clone"
    _git(tmp_path, "clone", "--no-local", path, str(clone))
    (clone / "reviewed.py").write_text("tested = True\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "tested change")
    expected = (clone / ".git" / "refs" / "heads" / "agent" / "odysseus" / "task").read_text().strip()
    with pytest.raises(service.WorktreeError) as caught:
        await service.request_publish("task", title="Reviewed", cfg=cfg, expected_head=expected)
    assert caught.value.code == "HEAD_MISMATCH"
    assert expected in str(caught.value)
    assert not (Path(cfg.state_dir) / "requests").exists()
    diagnosis = await service.diagnose("task", cfg=cfg, expected_head=expected, workspace=str(clone))
    assert diagnosis["code"] == "OBJECT_STORE_DRIFT"
    assert diagnosis["expected_head_available"] is False
    assert diagnosis["workspace_shares_objects"] is False
    assert diagnosis["next_action"]["repository"] == cfg.source_repo
    assert diagnosis["next_action"]["branch"] == "agent/odysseus/task"


async def test_named_status_does_not_scan_other_worktrees(cfg, monkeypatch):
    first = await service.ensure_worktree("task", cfg=cfg)
    await service.ensure_worktree("unrelated", cfg=cfg)
    scans = []
    original = service._worktree_dirs

    def scanned(config):
        scans.append(True)
        return original(config)

    monkeypatch.setattr(service, "_worktree_dirs", scanned)
    result = await service.status("task", cfg=cfg)
    assert [entry["path"] for entry in result["worktrees"]] == [first["path"]]
    assert scans == []


async def test_diagnosis_missing_pointer_preserves_files_and_names_host_boundary(cfg):
    info = await service.ensure_worktree("task", cfg=cfg)
    path = Path(info["path"])
    pointer = (path / ".git").read_text()
    (path / ".git").write_text("gitdir: /missing/repository/.git/worktrees/task\n")
    (path / "keep.txt").write_text("unsaved work\n")
    result = await service.diagnose("task", cfg=cfg)
    assert result["code"] == "WORKTREE_METADATA_INVALID"
    assert result["recovery"]["requires"] == "host_metadata_repair"
    assert (path / "keep.txt").read_text() == "unsaved work\n"
    # The read-only diagnostic must not repair pointers speculatively.
    assert (path / ".git").read_text() != pointer


async def test_matching_tested_head_keeps_existing_human_approval_flow(cfg, monkeypatch):
    from tests.src.agent_worktree.gitcmd.test_agent_worktree_service import _no_token, _fake_remote_head
    from src.agent_worktree import approval

    await _prepare_change(cfg)
    head = (await service.status("task", cfg=cfg))["head_sha"]
    _no_token(monkeypatch, cfg)
    monkeypatch.setattr(service, "_remote_head", _fake_remote_head(None))
    result = await service.request_publish("task", title="Reviewed", cfg=cfg, expected_head=head)
    request = approval.get_request(result["id"], cfg=cfg)
    assert request["status"] == "pending"
    assert request["head_sha"] == head


async def test_inventory_is_bounded_and_reports_omissions(cfg):
    root = Path(cfg.worktree_root)
    root.mkdir()
    for index in range(53):
        (root / f"unknown-{index:02}").mkdir()
    result = await service.status(cfg=cfg)
    assert len(result["worktrees"]) == 50
    assert result["worktrees_omitted"] == 3


async def test_diagnostic_can_verify_host_metadata_for_a_linked_worker_workspace(cfg):
    info = await service.ensure_worktree("task", cfg=cfg)
    result = await service.diagnose("task", cfg=cfg, workspace=info["path"],
                                    expected_head=info["head_sha"])
    assert result["code"] == "READY"
    assert result["workspace_shares_objects"] is True
    assert result["expected_head_available"] is True


async def test_named_status_has_constant_git_call_count(cfg, monkeypatch):
    await service.ensure_worktree("task", cfg=cfg)
    for name in ("one", "two", "three"):
        await service.ensure_worktree(name, cfg=cfg)
    calls = []
    original = service._git

    async def counted(config, args, **kwargs):
        calls.append((args, kwargs["cwd"]))
        return await original(config, args, **kwargs)

    monkeypatch.setattr(service, "_git", counted)
    result = await service.status("task", cfg=cfg)
    assert len(calls) == 3
    assert {cwd for _, cwd in calls} == {result["path"]}


async def test_diagnostic_never_lazy_fetches_from_a_promisor(cfg, tmp_path):
    path = await _prepare_change(cfg)
    remote = tmp_path / "promisor"
    _git(tmp_path, "clone", "--no-local", path, str(remote))
    (remote / "only-remote.py").write_text("not_local = True\n")
    _git(remote, "add", "-A")
    _git(remote, "commit", "-m", "only remote")
    expected = (remote / ".git" / "refs" / "heads" / "agent" / "odysseus" / "task").read_text().strip()
    _git(cfg.source_repo, "remote", "add", "origin", str(remote))
    _git(cfg.source_repo, "config", "remote.origin.promisor", "true")
    _git(cfg.source_repo, "config", "remote.origin.partialclonefilter", "blob:none")
    packs = Path(cfg.source_repo) / ".git" / "objects" / "pack"
    before = sorted(p.name for p in packs.iterdir())
    result = await service.diagnose("task", cfg=cfg, expected_head=expected)
    assert result["expected_head_available"] is False
    assert result["code"] == "OBJECT_STORE_DRIFT"
    assert sorted(p.name for p in packs.iterdir()) == before


async def test_existing_worktree_without_pointer_is_a_metadata_repair_not_start(cfg):
    info = await service.ensure_worktree("task", cfg=cfg)
    path = Path(info["path"])
    (path / ".git").unlink()
    (path / "keep.txt").write_text("unsaved work\n")
    result = await service.diagnose("task", cfg=cfg)
    assert result["code"] == "WORKTREE_METADATA_INVALID"
    assert result["recovery"]["requires"] == "host_metadata_repair"
    assert (path / "keep.txt").read_text() == "unsaved work\n"
