"""End-to-end worktree flow against a real temporary git repository.

Everything up to the network is exercised for real: worktree creation, commits,
the diff that feeds the approval request, and the re-verification publish does
before it spends an approval. Only the push and the GitHub API are substituted,
and the substitutes assert on what they were handed.
"""

import dataclasses
import os
import subprocess

import pytest

from src.agent_worktree import approval as approval_mod
from src.agent_worktree import service
from src.agent_worktree.config import WorktreeConfig
from src.agent_worktree.gitcmd import auth_env

pytestmark = pytest.mark.area_security

git_required = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git is not installed",
)


def _git(cwd, *args):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.test",
            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.test",
        },
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "dev")
    (root / "README.md").write_text("hello\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def cfg(tmp_path, repo):
    return WorktreeConfig(
        publish_enabled=True,
        repo_slug="acme/widgets",
        source_repo=str(repo),
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


async def _prepare_change(cfg, filename="src/feature.py", body="print('hi')\n"):
    info = await service.ensure_worktree("task", cfg=cfg)
    path = info["path"]
    target = os.path.join(path, filename)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(body)
    await service.commit("task", "add feature", cfg=cfg)
    return path


@git_required
async def test_worktree_is_created_on_an_agent_branch(cfg):
    info = await service.ensure_worktree("task", cfg=cfg)
    assert info["branch"] == "agent/odysseus/task"
    assert info["exists"] is True
    assert os.path.realpath(info["path"]).startswith(os.path.realpath(cfg.worktree_root))


@git_required
async def test_worktree_is_reused_not_recreated(cfg):
    first = await service.ensure_worktree("task", cfg=cfg)
    second = await service.ensure_worktree("task", cfg=cfg)
    assert first["path"] == second["path"]


@git_required
async def test_a_name_outside_the_namespace_is_refused(cfg):
    with pytest.raises(service.WorktreeError):
        await service.ensure_worktree("../../escape", cfg=cfg)


@git_required
async def test_commit_and_diff_report_the_change(cfg):
    await _prepare_change(cfg)
    summary = await service.diff_summary("task", cfg=cfg)
    assert summary["changed_files"] == ["src/feature.py"]
    assert summary["dirty"] is False
    assert summary["sensitive"] == {}
    assert len(summary["head_sha"]) in (40, 64)


@git_required
async def test_diff_flags_a_sensitive_file(cfg):
    await _prepare_change(cfg, filename=".github/workflows/ci.yml", body="on: push\n")
    summary = await service.diff_summary("task", cfg=cfg)
    assert "workflows" in summary["sensitive"]
    assert summary["sensitive_digest"]


@git_required
async def test_request_publish_is_blocked_when_the_feature_is_off(cfg):
    await _prepare_change(cfg)
    off = dataclasses.replace(cfg, publish_enabled=False)
    with pytest.raises(service.WorktreeError, match="publishing is not available"):
        await service.request_publish("task", title="x", cfg=off)


@git_required
async def test_request_publish_refuses_an_uncommitted_worktree(cfg, monkeypatch):
    path = await _prepare_change(cfg)
    with open(os.path.join(path, "src", "feature.py"), "a", encoding="utf-8") as fh:
        fh.write("# drift\n")
    monkeypatch.setattr(service, "_remote_head", _fake_remote_head(None))
    with pytest.raises(service.WorktreeError, match="uncommitted"):
        await service.request_publish("task", title="x", cfg=cfg)


def _fake_remote_head(value):
    async def _inner(cfg, branch, token):
        return value
    return _inner


def _no_token(monkeypatch, cfg):
    async def _resolve(_cfg):
        return "ghs_" + "t" * 36
    monkeypatch.setattr("src.agent_worktree.github.resolve_token", _resolve)


@git_required
async def test_request_publish_records_but_does_not_push(cfg, monkeypatch):
    await _prepare_change(cfg)
    _no_token(monkeypatch, cfg)
    monkeypatch.setattr(service, "_remote_head", _fake_remote_head(None))

    pushes = []
    monkeypatch.setattr(service, "run_git", _push_watching_git(pushes))

    view = await service.request_publish("task", title="Add feature", cfg=cfg)
    assert view["status"] == "pending"
    assert view["remote_state"] == "absent"
    assert view["changed_files"] == ["src/feature.py"]
    assert pushes == []


def _push_watching_git(sink, *, on_push=None):
    """Delegate every git call to the real runner, but record pushes.

    Patching run_git wholesale would break the diff that request_publish needs,
    so the wrapper only intervenes on `push`.
    """
    from src.agent_worktree.gitcmd import run_git as real_run_git

    async def _run(args, **kwargs):
        arg_list = list(args)
        if arg_list and arg_list[0] == "push":
            sink.append({
                "args": arg_list,
                "env": kwargs.get("extra_env") or {},
                "secrets": kwargs.get("secrets") or (),
            })
            if on_push is not None:
                return on_push
            raise AssertionError("this step must not push")
        return await real_run_git(args, **kwargs)

    return _run


@git_required
async def test_publish_requires_a_human_approval_code(cfg, monkeypatch):
    await _prepare_change(cfg)
    _no_token(monkeypatch, cfg)
    monkeypatch.setattr(service, "_remote_head", _fake_remote_head(None))
    view = await service.request_publish("task", title="Add feature", cfg=cfg)

    with pytest.raises(approval_mod.ApprovalError, match="not been approved"):
        await service.publish(view["id"], "guessed-code", cfg=cfg)


@git_required
async def test_full_flow_pushes_and_opens_a_draft_pr(cfg, monkeypatch):
    await _prepare_change(cfg)
    _no_token(monkeypatch, cfg)
    monkeypatch.setattr(service, "_remote_head", _fake_remote_head(None))
    view = await service.request_publish("task", title="Add feature", cfg=cfg)

    code, _ = approval_mod.grant(view["id"], granted_by="alice", cfg=cfg)

    from src.agent_worktree.gitcmd import GitResult

    pushes = []
    created = {}

    async def fake_create_pr(_cfg, _token, **kwargs):
        created.update(kwargs)
        return {"number": 7, "url": "https://github.test/pr/7", "draft": True, "state": "open"}

    async def fake_find_pr(_cfg, _token, _branch):
        return None

    monkeypatch.setattr(
        service,
        "run_git",
        _push_watching_git(pushes, on_push=GitResult(code=0, stdout="", stderr="")),
    )
    monkeypatch.setattr("src.agent_worktree.github.create_draft_pr", fake_create_pr)
    monkeypatch.setattr("src.agent_worktree.github.find_open_pr", fake_find_pr)

    result = await service.publish(view["id"], code, cfg=cfg)

    assert result["pull_request"]["number"] == 7
    assert result["pull_request"]["draft"] is True
    assert created["base_branch"] == "dev"
    assert created["head_branch"] == "agent/odysseus/task"

    # The push is an argument array against the derived remote, on the agent ref.
    assert len(pushes) == 1
    pushed = pushes[0]
    assert pushed["args"][0] == "push"
    assert "https://github.com/acme/widgets.git" in pushed["args"]
    # The approved commit object is pushed, not the branch ref: a ref could
    # move between verification and push, a SHA cannot.
    approved = (await service.diff_summary("task", cfg=cfg))["head_sha"]
    assert f"{approved}:refs/heads/agent/odysseus/task" in pushed["args"]
    # No credential in argv; it travels via GIT_CONFIG_* and is registered for
    # scrubbing from any captured output.
    assert not any("ghs_" in str(a) for a in pushed["args"])
    assert pushed["env"].get("GIT_CONFIG_KEY_0", "").endswith(".extraheader")
    assert pushed["secrets"]

    # Single use: the same code cannot publish again.
    with pytest.raises(approval_mod.ApprovalError, match="already used"):
        await service.publish(view["id"], code, cfg=cfg)


@git_required
async def test_publish_refuses_when_the_commit_moved_after_approval(cfg, monkeypatch):
    path = await _prepare_change(cfg)
    _no_token(monkeypatch, cfg)
    monkeypatch.setattr(service, "_remote_head", _fake_remote_head(None))
    view = await service.request_publish("task", title="Add feature", cfg=cfg)
    code, _ = approval_mod.grant(view["id"], cfg=cfg)

    # The agent keeps working after the human looked at the change.
    with open(os.path.join(path, "src", "extra.py"), "w", encoding="utf-8") as fh:
        fh.write("# more\n")
    await service.commit("task", "sneak in more", cfg=cfg)

    with pytest.raises(approval_mod.ApprovalError, match="bound to a different"):
        await service.publish(view["id"], code, cfg=cfg)


@git_required
async def test_publish_refuses_when_the_remote_branch_moved(cfg, monkeypatch):
    await _prepare_change(cfg)
    _no_token(monkeypatch, cfg)
    monkeypatch.setattr(service, "_remote_head", _fake_remote_head(None))
    view = await service.request_publish("task", title="Add feature", cfg=cfg)
    code, _ = approval_mod.grant(view["id"], cfg=cfg)

    monkeypatch.setattr(service, "_remote_head", _fake_remote_head("c" * 40))
    with pytest.raises(service.WorktreeError, match="remote branch moved"):
        await service.publish(view["id"], code, cfg=cfg)


@git_required
async def test_sensitive_change_cannot_be_approved_without_acknowledgement(cfg, monkeypatch):
    await _prepare_change(cfg, filename=".github/workflows/ci.yml", body="on: push\n")
    _no_token(monkeypatch, cfg)
    monkeypatch.setattr(service, "_remote_head", _fake_remote_head(None))
    view = await service.request_publish("task", title="CI tweak", cfg=cfg)
    assert "workflows" in view["sensitive"]
    with pytest.raises(approval_mod.ApprovalError, match="allow-sensitive"):
        approval_mod.grant(view["id"], cfg=cfg)


def test_auth_env_keeps_the_token_out_of_argv_and_scopes_it():
    env = auth_env("ghs_secret_token_value", "https://github.com/acme/widgets.git")
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/acme/widgets.git.extraheader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    # The raw token is not in the header value; it is base64 of user:token.
    assert "ghs_secret_token_value" not in env["GIT_CONFIG_VALUE_0"]


def test_auth_env_pins_tls_and_proxy_so_repo_config_cannot_redirect_it():
    env = auth_env("ghs_secret_token_value", "https://github.com/acme/widgets.git")
    pairs = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert pairs["http.sslVerify"] == "true"
    assert pairs["http.proxy"] == ""


@git_required
async def test_publish_refuses_a_detached_head(cfg, monkeypatch):
    """HEAD parked on the approved commit while the branch ref moved on.

    Without this check the operator would approve the commit at HEAD and the
    branch's newer commit would be what got published.
    """
    path = await _prepare_change(cfg)
    _no_token(monkeypatch, cfg)
    monkeypatch.setattr(service, "_remote_head", _fake_remote_head(None))
    view = await service.request_publish("task", title="Add feature", cfg=cfg)
    code, _ = approval_mod.grant(view["id"], cfg=cfg)

    approved = view["head_sha"]
    with open(os.path.join(path, "src", "extra.py"), "w", encoding="utf-8") as fh:
        fh.write("# hidden\n")
    await service.commit("task", "hidden change", cfg=cfg)
    _git(path, "checkout", "--quiet", "--detach", approved)

    with pytest.raises(service.WorktreeError, match="detached HEAD"):
        await service.publish(view["id"], code, cfg=cfg)


@git_required
async def test_the_credentialed_push_does_not_run_inside_the_agent_worktree(cfg, monkeypatch):
    """git reads repo-local config from the cwd's gitdir, and the agent owns
    the worktree's. The push therefore runs from the operator's checkout."""
    await _prepare_change(cfg)
    _no_token(monkeypatch, cfg)
    monkeypatch.setattr(service, "_remote_head", _fake_remote_head(None))
    view = await service.request_publish("task", title="Add feature", cfg=cfg)
    code, _ = approval_mod.grant(view["id"], cfg=cfg)

    from src.agent_worktree.gitcmd import GitResult

    pushes = []
    seen_cwd = {}

    async def watch(args, **kwargs):
        arg_list = list(args)
        if arg_list and arg_list[0] == "push":
            pushes.append(arg_list)
            seen_cwd["cwd"] = kwargs.get("cwd")
            return GitResult(code=0, stdout="", stderr="")
        from src.agent_worktree.gitcmd import run_git as real

        return await real(args, **kwargs)

    async def fake_create_pr(_cfg, _token, **kwargs):
        return {"number": 1, "url": "u", "draft": True, "state": "open"}

    async def fake_find_pr(_cfg, _token, _branch):
        return None

    monkeypatch.setattr(service, "run_git", watch)
    monkeypatch.setattr("src.agent_worktree.github.create_draft_pr", fake_create_pr)
    monkeypatch.setattr("src.agent_worktree.github.find_open_pr", fake_find_pr)

    await service.publish(view["id"], code, cfg=cfg)
    assert seen_cwd["cwd"] == cfg.source_repo
    assert not str(seen_cwd["cwd"]).startswith(cfg.worktree_root)


def test_auth_env_is_empty_without_a_token():
    assert auth_env(None, "https://github.com/acme/widgets.git") == {}
    assert auth_env("", "https://github.com/acme/widgets.git") == {}
