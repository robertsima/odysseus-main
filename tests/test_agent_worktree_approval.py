"""Approval grants: short-lived, single-use, and bound to the exact change.

The property under test is that an approval cannot be reused, cannot outlive its
window, and cannot be carried over to a different repository, branch, commit,
file set, or sensitive-path set.
"""

import dataclasses
import time

import pytest

from src.agent_worktree import approval as approval_mod
from src.agent_worktree.config import WorktreeConfig

pytestmark = pytest.mark.area_security


SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture
def cfg(tmp_path):
    return WorktreeConfig(
        publish_enabled=True,
        repo_slug="acme/widgets",
        source_repo=str(tmp_path / "repo"),
        worktree_root=str(tmp_path / "wt"),
        state_dir=str(tmp_path / "state"),
        base_branch="dev",
        approval_ttl_s=900,
        api_base="https://api.github.test",
        app_id="",
        installation_id="",
        private_key_path="",
        fallback_token_env="ODYSSEUS_AGENT_GITHUB_TOKEN",
    )


def _request(cfg, *, files=("src/a.py",), sensitive=None, sha=SHA_A):
    sensitive = sensitive or {}
    from src.agent_worktree import sensitive as sensitive_mod
    from src.agent_worktree.service import files_digest

    return approval_mod.create_request(
        repo=cfg.repo_slug,
        branch="agent/odysseus/task",
        head_sha=sha,
        base_branch="dev",
        title="Fix the thing",
        body="",
        changed_files=list(files),
        files_digest=files_digest(list(files)),
        sensitive=sensitive,
        sensitive_digest=sensitive_mod.digest(sensitive),
        cfg=cfg,
    )


def _consume(cfg, record, code, **overrides):
    from src.agent_worktree import sensitive as sensitive_mod

    kwargs = {
        "repo": record["repo"],
        "branch": record["branch"],
        "head_sha": record["head_sha"],
        "files_digest": record["files_digest"],
        "sensitive_digest": sensitive_mod.digest(record["sensitive"]),
        "has_sensitive": bool(record["sensitive"]),
    }
    kwargs.update(overrides)
    return approval_mod.consume(record["id"], code, cfg=cfg, **kwargs)


def test_a_fresh_request_cannot_be_published(cfg):
    record = _request(cfg)
    with pytest.raises(approval_mod.ApprovalError, match="not been approved"):
        _consume(cfg, record, "anything")


def test_granted_code_publishes_once_then_never_again(cfg):
    record = _request(cfg)
    code, _ = approval_mod.grant(record["id"], granted_by="alice", cfg=cfg)
    _consume(cfg, record, code)
    with pytest.raises(approval_mod.ApprovalError, match="already used"):
        _consume(cfg, record, code)


def test_the_plaintext_code_is_never_persisted(cfg):
    record = _request(cfg)
    code, _ = approval_mod.grant(record["id"], cfg=cfg)
    stored = open(approval_mod._record_path(cfg, record["id"]), encoding="utf-8").read()
    assert code not in stored
    assert "code_hash" in stored


def test_public_view_hides_the_hash_and_salt(cfg):
    record = _request(cfg)
    approval_mod.grant(record["id"], cfg=cfg)
    view = approval_mod.get_request(record["id"], cfg=cfg)
    assert view["grant"] is not None
    assert "code_hash" not in view["grant"]
    assert "salt" not in view["grant"]


def test_a_wrong_code_is_rejected_and_does_not_spend_the_grant(cfg):
    record = _request(cfg)
    code, _ = approval_mod.grant(record["id"], cfg=cfg)
    with pytest.raises(approval_mod.ApprovalError, match="does not match"):
        _consume(cfg, record, code + "x")
    # The real code still works: a failed guess must not burn the approval.
    _consume(cfg, record, code)


def test_an_expired_grant_cannot_be_used(cfg, monkeypatch):
    short = dataclasses.replace(cfg, approval_ttl_s=60)
    record = _request(short)
    code, _ = approval_mod.grant(record["id"], cfg=short)
    later = time.time() + 120
    monkeypatch.setattr(approval_mod, "_now", lambda: later)
    with pytest.raises(approval_mod.ApprovalError, match="expired"):
        _consume(short, record, code)


def test_editing_the_expiry_on_disk_does_not_extend_a_grant(cfg, monkeypatch):
    """A hand-edited record is rejected outright rather than honoured."""
    short = dataclasses.replace(cfg, approval_ttl_s=60)
    record = _request(short)
    code, _ = approval_mod.grant(record["id"], cfg=short)
    stored = approval_mod._load(short, record["id"])
    stored["grant"]["expires_at"] = time.time() + 10_000_000
    approval_mod._save(short, stored)
    # The edit breaks the grant's MAC, so the record stops counting as an
    # approval at all rather than counting as a longer-lived one.
    assert approval_mod.get_request(record["id"], cfg=short)["status"] == "pending"
    with pytest.raises(approval_mod.ApprovalError, match="not been approved"):
        _consume(short, record, code)


def test_a_forged_grant_is_rejected(cfg):
    """Writing a 'granted' record by hand must not authorize a publish.

    The state directory is deny-listed for the agent's file tools, but the MAC
    is the check that does not depend on that deny list holding.
    """
    import hashlib

    record = _request(cfg)
    stored = approval_mod._load(cfg, record["id"])
    stored["status"] = "granted"
    stored["grant"] = {
        "salt": "aa",
        "code_hash": hashlib.sha256(b"aa:letmein").hexdigest(),
        "granted_at": time.time(),
        "expires_at": time.time() + 10_000,
        "allow_sensitive": True,
        "granted_by": "not-a-human",
        "used_at": None,
        "bound": {
            "repo": record["repo"],
            "branch": record["branch"],
            "head_sha": record["head_sha"],
            "files_digest": record["files_digest"],
            "sensitive_digest": record["sensitive_digest"],
        },
        "mac": "0" * 64,
    }
    approval_mod._save(cfg, stored)
    assert approval_mod.get_request(record["id"], cfg=cfg)["status"] == "pending"
    with pytest.raises(approval_mod.ApprovalError):
        _consume(cfg, record, "letmein")


def test_a_grant_with_no_bindings_is_rejected(cfg):
    """An empty `bound` must fail closed, not skip every binding check."""
    record = _request(cfg)
    code, _ = approval_mod.grant(record["id"], cfg=cfg)
    stored = approval_mod._load(cfg, record["id"])
    stored["grant"]["bound"] = {}
    approval_mod._save(cfg, stored)
    assert approval_mod.get_request(record["id"], cfg=cfg)["status"] == "pending"
    with pytest.raises(approval_mod.ApprovalError, match="not been approved"):
        _consume(cfg, record, code)
    with pytest.raises(approval_mod.ApprovalError, match="not bound"):
        approval_mod._verified_grant(cfg, approval_mod._load(cfg, record["id"]))


def test_a_grant_missing_one_binding_is_rejected(cfg):
    record = _request(cfg)
    code, _ = approval_mod.grant(record["id"], cfg=cfg)
    stored = approval_mod._load(cfg, record["id"])
    stored["grant"]["bound"].pop("head_sha")
    approval_mod._save(cfg, stored)
    assert approval_mod.get_request(record["id"], cfg=cfg)["status"] == "pending"
    with pytest.raises(approval_mod.ApprovalError, match="not been approved"):
        _consume(cfg, record, code)
    with pytest.raises(approval_mod.ApprovalError, match="not bound"):
        approval_mod._verified_grant(cfg, approval_mod._load(cfg, record["id"]))


def test_the_mac_key_is_not_world_readable(cfg):
    import os
    import stat

    record = _request(cfg)
    approval_mod.grant(record["id"], cfg=cfg)
    mode = os.stat(approval_mod._key_path(cfg)).st_mode
    assert not (mode & (stat.S_IRGRP | stat.S_IROTH))


@pytest.mark.parametrize(
    "field,value",
    [
        ("repo", "evil/widgets"),
        ("branch", "agent/odysseus/other"),
        ("head_sha", SHA_B),
        ("files_digest", "0" * 64),
        ("sensitive_digest", "0" * 64),
    ],
)
def test_an_approval_does_not_transfer_to_a_different_change(cfg, field, value):
    record = _request(cfg)
    code, _ = approval_mod.grant(record["id"], cfg=cfg)
    with pytest.raises(approval_mod.ApprovalError, match="bound to a different"):
        _consume(cfg, record, code, **{field: value})


def test_sensitive_change_needs_the_second_acknowledgement(cfg):
    record = _request(cfg, files=(".github/workflows/ci.yml",),
                      sensitive={"workflows": [".github/workflows/ci.yml"]})
    with pytest.raises(approval_mod.ApprovalError, match="allow-sensitive"):
        approval_mod.grant(record["id"], cfg=cfg)


def test_sensitive_change_publishes_once_acknowledged(cfg):
    record = _request(cfg, files=(".github/workflows/ci.yml",),
                      sensitive={"workflows": [".github/workflows/ci.yml"]})
    code, view = approval_mod.grant(record["id"], allow_sensitive=True, cfg=cfg)
    assert view["grant"]["allow_sensitive"] is True
    _consume(cfg, record, code)


def test_a_non_sensitive_grant_cannot_cover_a_change_that_became_sensitive(cfg):
    """Grant made for an ordinary change; the live change now has sensitive files.

    The digest binding catches it first, and the explicit acknowledgement check
    is the second layer behind it.
    """
    record = _request(cfg)
    code, _ = approval_mod.grant(record["id"], cfg=cfg)
    with pytest.raises(approval_mod.ApprovalError):
        _consume(cfg, record, code, has_sensitive=True, sensitive_digest="1" * 64)


def test_revoked_requests_cannot_be_granted_or_used(cfg):
    record = _request(cfg)
    code, _ = approval_mod.grant(record["id"], cfg=cfg)
    approval_mod.revoke(record["id"], cfg=cfg)
    with pytest.raises(approval_mod.ApprovalError, match="revoked"):
        _consume(cfg, record, code)
    with pytest.raises(approval_mod.ApprovalError, match="revoked"):
        approval_mod.grant(record["id"], cfg=cfg)


def test_stale_requests_expire_without_a_grant(cfg):
    record = _request(cfg)
    stored = approval_mod._load(cfg, record["id"])
    stored["expires_at"] = time.time() - 1
    approval_mod._save(cfg, stored)
    assert approval_mod.get_request(record["id"], cfg=cfg)["status"] == "expired"
    with pytest.raises(approval_mod.ApprovalError, match="expired"):
        approval_mod.grant(record["id"], cfg=cfg)


def test_request_ids_cannot_traverse_the_state_directory(cfg):
    for bad in ("../../etc/passwd", "a/b", "..", "x\x00y"):
        with pytest.raises(approval_mod.ApprovalError):
            approval_mod.get_request(bad, cfg=cfg)


def test_missing_request_is_an_error_not_an_approval(cfg):
    with pytest.raises(approval_mod.ApprovalError, match="no approval request"):
        approval_mod.get_request("0" * 32, cfg=cfg)


def test_grant_rebinds_from_the_record_at_grant_time(cfg):
    """Editing the record between request and grant must not change what is bound."""
    record = _request(cfg)
    stored = approval_mod._load(cfg, record["id"])
    stored["head_sha"] = SHA_B
    approval_mod._save(cfg, stored)
    code, _ = approval_mod.grant(record["id"], cfg=cfg)
    # The grant bound SHA_B, so publishing the original SHA_A is refused.
    with pytest.raises(approval_mod.ApprovalError, match="bound to a different"):
        _consume(cfg, record, code, head_sha=SHA_A)


def test_listing_shows_status_transitions(cfg):
    record = _request(cfg)
    assert approval_mod.list_requests(cfg=cfg)[0]["status"] == "pending"
    code, _ = approval_mod.grant(record["id"], cfg=cfg)
    assert approval_mod.list_requests(cfg=cfg)[0]["status"] == "granted"
    _consume(cfg, record, code)
    assert approval_mod.list_requests(cfg=cfg)[0]["status"] == "used"
