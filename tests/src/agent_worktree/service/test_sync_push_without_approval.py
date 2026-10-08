"""publish_sync: a base-sync merge of an approved, published head pushes without a new code.

2026-10-07/08: every push needed a fresh approval code, including one that
only merged the base: 3-4 approvals per PR, one wait 21 minutes. The
exemption must not become a way around approval, so each negative case here
is a change a person never saw: an extra commit, a hand-resolved conflict,
extra edits inside the merge, another ref merged, another branch or owner, a
forged chain entry, an approval past its window.
"""

import json
import os
import time

import pytest

from src.agent_worktree import approval as approval_mod
from src.agent_worktree import service
from src.agent_worktree.gitcmd import GitResult
from tests.src.agent_worktree.gitcmd.test_agent_worktree_service import _push_watching_git
from tests.src.agent_worktree.service.test_base_sync import (  # noqa: F401 - fixtures
    cfg, git, git_required, local_fetch, repo, start_with_change, upstream, upstream_commit, write,
)

pytestmark = [pytest.mark.security, git_required]

BRANCH = "agent/odysseus/task"


@pytest.fixture
def remote(cfg, upstream, monkeypatch):
    """GitHub as seen by the host: branch heads, the token, PRs, and pushes."""
    state = {"heads": {"dev": git(upstream, "rev-parse", "dev")}, "pushes": []}

    async def remote_head(_cfg, branch, _token):
        return state["heads"].get(branch)

    async def token(_cfg):
        return "ghs_" + "t" * 36

    async def find_pr(_cfg, _token, _branch):
        return {"number": 7, "url": "https://github.test/pr/7", "draft": True, "state": "open"}

    monkeypatch.setattr(service, "_remote_head", remote_head)
    monkeypatch.setattr("src.agent_worktree.github.resolve_token", token)
    monkeypatch.setattr("src.agent_worktree.github.find_open_pr", find_pr)
    monkeypatch.setattr(service, "run_git", _push_watching_git(
        state["pushes"], on_push=GitResult(code=0, stdout="", stderr="")))
    return state


async def published(cfg, remote, owner="alice", name="task", **change):
    """An agent change approved by a person and pushed, as the flow leaves it."""
    path = await start_with_change(cfg, name=name, **change)
    view = await service.request_publish(name, title="Change", requested_by=owner, cfg=cfg)
    code, _ = approval_mod.grant(view["id"], granted_by=owner, cfg=cfg)
    await service.publish(view["id"], code, cfg=cfg)
    approved = git(path, "rev-parse", "HEAD")
    remote["heads"][f"agent/odysseus/{name}"] = approved
    remote["pushes"].clear()
    return path, approved, view["id"]


async def advance_base(cfg, upstream, remote, relative="docs/base.md", text="from dev\n"):
    tip = upstream_commit(upstream, relative, text)
    remote["heads"]["dev"] = tip
    return tip


async def refused(cfg, remote, owner="alice", name="task"):
    with pytest.raises(service.WorktreeError) as caught:
        await service.publish_sync(name, owner=owner, cfg=cfg)
    assert caught.value.code == "SYNC_NEEDS_APPROVAL"
    assert remote["pushes"] == []
    return str(caught.value)


async def test_clean_base_merge_pushes_without_a_code_once_per_step(cfg, upstream, remote, local_fetch):
    path, approved, request_id = await published(cfg, remote)
    tip = await advance_base(cfg, upstream, remote)
    merged = (await service.sync("task", cfg=cfg))["head_sha"]

    out = await service.publish_sync("task", owner="alice", cfg=cfg)

    assert out["approval_exempt"] == "base_sync" and out["request_id"] == request_id
    assert (out["head_sha"], out["approved_head"], out["base_sha"]) == (merged, approved, tip)
    [push] = remote["pushes"]
    assert f"{merged}:refs/heads/{BRANCH}" in push["args"]
    assert f"--force-with-lease=refs/heads/{BRANCH}:{approved}" in push["args"]
    record = approval_mod._load(cfg, request_id)
    assert [(e["from"], e["to"], e["base_sha"]) for e in record["sync_pushes"]] == [(approved, merged, tip)]

    # Single use: the same step cannot be pushed again.
    remote["pushes"].clear()
    assert "first parent" in await refused(cfg, remote)

    # The next base sync continues from the pushed merge.
    remote["heads"][BRANCH] = merged
    tip2 = await advance_base(cfg, upstream, remote, "docs/later.md", "later\n")
    merged2 = (await service.sync("task", cfg=cfg))["head_sha"]
    out2 = await service.publish_sync("task", owner="alice", cfg=cfg)
    assert (out2["approved_head"], out2["head_sha"], out2["base_sha"]) == (merged, merged2, tip2)


async def test_an_extra_commit_after_the_merge_needs_approval(cfg, upstream, remote, local_fetch):
    path, _approved, _id = await published(cfg, remote)
    await advance_base(cfg, upstream, remote)
    await service.sync("task", cfg=cfg)
    write(path, "sneaked.py", "x = 2\n")
    await service.commit("task", "more work", cfg=cfg)
    await refused(cfg, remote)


async def test_extra_edits_inside_a_clean_merge_need_approval(cfg, upstream, remote, local_fetch):
    path, _approved, _id = await published(cfg, remote)
    await advance_base(cfg, upstream, remote)
    await service.sync("task", cfg=cfg)
    write(path, "sneaked.py", "x = 2\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "--amend", "--no-edit")
    assert "clean automatic merge" in await refused(cfg, remote)


async def test_a_merge_with_hand_resolved_conflicts_needs_approval(cfg, upstream, remote, local_fetch):
    path, _approved, _id = await published(cfg, remote, relative="README.md",
                                           text="one\nbranch\nthree\n")
    await advance_base(cfg, upstream, remote, "README.md", "one\ndev\nthree\n")
    assert (await service.sync("task", cfg=cfg))["result"] == "conflicted"
    write(path, "README.md", "one\nbranch and dev\nthree\n")
    assert (await service.commit("task", "merge dev", cfg=cfg))["merge_concluded"]
    assert "clean automatic merge" in await refused(cfg, remote)


async def test_merging_a_ref_other_than_the_base_tip_needs_approval(cfg, upstream, remote, repo):
    path, _approved, _id = await published(cfg, remote)
    git(upstream, "checkout", "-q", "-b", "feature")
    upstream_commit(upstream, "feature.md", "not on dev\n", branch="feature")
    git(repo, "fetch", "-q", "origin", "+refs/heads/feature:refs/remotes/origin/feature")
    git(path, "merge", "-q", "--no-edit", "origin/feature")
    assert "not the tip of dev" in await refused(cfg, remote)


async def test_another_owner_cannot_use_the_approval(cfg, upstream, remote, local_fetch):
    await published(cfg, remote, owner="alice")
    await advance_base(cfg, upstream, remote)
    await service.sync("task", cfg=cfg)
    assert "different person" in await refused(cfg, remote, owner="bob")
    await refused(cfg, remote, owner=None)


async def test_another_branch_cannot_use_the_approval(cfg, upstream, remote, local_fetch):
    _path, approved, _id = await published(cfg, remote)
    await service.ensure_worktree("other", cfg=cfg, base=approved)
    remote["heads"]["agent/odysseus/other"] = approved
    await advance_base(cfg, upstream, remote)
    merged = await service.sync("other", cfg=cfg)
    assert merged["result"] == "merged" and merged["previous_head"] == approved
    await refused(cfg, remote, name="other")


async def test_a_forged_chain_entry_is_rejected(cfg, upstream, remote, local_fetch):
    path, approved, request_id = await published(cfg, remote)
    write(path, "unapproved.py", "x = 3\n")
    unapproved = (await service.commit("task", "never approved", cfg=cfg))["head_sha"]
    await advance_base(cfg, upstream, remote)
    await service.sync("task", cfg=cfg)
    assert "first parent" in await refused(cfg, remote)

    record_path = os.path.join(cfg.state_dir, "requests", f"{request_id}.json")
    with open(record_path, encoding="utf-8") as fh:
        record = json.load(fh)
    record["sync_pushes"] = [{"from": approved, "to": unapproved, "base_sha": approved,
                              "at": time.time(), "mac": "0" * 64}]
    with open(record_path, "w", encoding="utf-8") as fh:
        json.dump(record, fh)
    assert "integrity" in await refused(cfg, remote)


async def test_an_approval_past_its_window_is_not_extended(cfg, upstream, remote, local_fetch, monkeypatch):
    await published(cfg, remote)
    await advance_base(cfg, upstream, remote)
    await service.sync("task", cfg=cfg)
    later = time.time() + approval_mod.SYNC_EXEMPTION_WINDOW_S + 3600
    monkeypatch.setattr(approval_mod, "_now", lambda: later)
    assert "too old" in await refused(cfg, remote)
