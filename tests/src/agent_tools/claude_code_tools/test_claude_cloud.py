"""Claude Code cloud runner: delegation to Claude Code in GitHub Actions.

Odysseus dispatches a workflow in an allowlisted repository and follows it;
Claude's credential lives only in that repository's secrets.
"""
import asyncio
import io
import json
import re
import zipfile
from pathlib import Path

import pytest

from src import claude_cloud as cc
from tests import REPO_ROOT

ROOT = REPO_ROOT


def _run(coro):
    return asyncio.run(coro)


class FakeGitHub:
    """Enough of the REST API for dispatch -> run -> artifact/branch/PR."""

    def __init__(self):
        self.calls = []
        self.run = None          # the workflow run, once "created"
        self.pr = None
        self.branch_exists = True

    async def __call__(self, method, path, *, params=None, json_body=None, raw=False):
        self.calls.append((method, path, params, json_body))
        if method == "GET" and path == "/repos/acme/app":
            return {"default_branch": "main"}
        if method == "GET" and path == "/repos/acme/hub":
            return {"default_branch": "dev"}
        if method == "GET" and path == "/repos/zeta/new":
            return {"default_branch": "trunk"}
        if method == "GET" and path == "/repos/zeta/secret":
            return {"default_branch": "main", "private": True}
        if method == "GET" and path == "/repos/acme/public-hub":
            return {"default_branch": "main", "private": False}
        if method == "GET" and path == "/repos/zeta/new/pulls":
            return [self.pr] if self.pr else []
        if method == "POST" and path.endswith("/dispatches"):
            short = json_body["inputs"]["task_id"]
            self.run = {"id": 77, "status": "queued", "conclusion": None,
                        "display_title": f"Odysseus task {short}",
                        "html_url": "https://github.com/acme/app/actions/runs/77"}
            return None
        if method == "GET" and path.endswith("/runs"):
            return {"workflow_runs": [{"id": 1, "display_title": "Odysseus task other"}]
                    + ([self.run] if self.run else [])}
        if method == "GET" and path.endswith("/actions/runs/77/artifacts"):
            return {"artifacts": [{"name": "odysseus-result", "expired": False,
                                   "archive_download_url": "https://api.github.com/artifact/1/zip"}]}
        if method == "GET" and path == "/repos/acme/app/pulls":
            return [self.pr] if self.pr else []
        if method == "GET" and "/branches/" in path:
            if not self.branch_exists:
                raise cc.CloudError("GitHub GET branches failed (404)")
            return {"name": path.rsplit("/branches/", 1)[1]}
        if method == "POST" and path.endswith("/cancel"):
            self.run["status"] = "completed"
            self.run["conclusion"] = "cancelled"
            return None
        if method == "GET" and "/actions/workflows/" in path:
            return {"id": 5}
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    from src import constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cc, "_tasks", {})
    monkeypatch.setattr(cc, "_loaded", True)
    monkeypatch.setattr(cc, "_start_watcher", lambda task_id: None)
    settings = {"claude_cloud_repositories": ["acme/app", "https://github.com/acme/other.git"],
                "claude_cloud_workflow": "odysseus-claude.yml"}
    monkeypatch.setattr(cc, "_setting", lambda key, default=None: settings.get(key, default))
    gh = FakeGitHub()
    monkeypatch.setattr(cc, "_gh", gh)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("claude-result.md", "Fixed the parser and added a test.")
    monkeypatch.setattr(cc, "_download_artifact", lambda url: _async(buf.getvalue()))
    events = []
    monkeypatch.setattr(cc.activity, "run_started", lambda *a, **k: events.append(("started", a, k)) or "run-1")
    monkeypatch.setattr(cc.activity, "run_finished", lambda *a, **k: events.append(("finished", a, k)))
    monkeypatch.setattr(cc.activity, "publish", lambda *a, **k: events.append(("publish", a, k)))
    return gh, events, settings


async def _async(value):
    return value


def test_repositories_are_normalised_and_allowlisted(cloud):
    assert cc.repositories() == ["acme/app", "acme/other"]
    assert cc.is_cloud_repository("ACME/app")
    assert not cc.is_cloud_repository("evil/app")


def test_dispatch_sends_the_task_and_records_it(cloud):
    gh, events, _ = cloud
    record = _run(cc.dispatch("acme/app", "Fix the parser", owner="alice", session_id="s1"))
    method, path, _, body = [c for c in gh.calls if c[0] == "POST"][0]
    assert path == "/repos/acme/app/actions/workflows/odysseus-claude.yml/dispatches"
    assert body["ref"] == "main"
    assert body["inputs"]["prompt"] == "Fix the parser"
    assert record["task_id"] == "cloud-" + body["inputs"]["task_id"]
    assert record["branch"] == "claude/odysseus-" + body["inputs"]["task_id"]
    assert record["status"] == "queued"
    assert events[0][0] == "started" and events[0][1][:2] == ("s1", "claude_code")


def test_dispatch_refuses_repositories_that_are_not_allowlisted(cloud):
    with pytest.raises(cc.CloudError, match="not an allowlisted"):
        _run(cc.dispatch("evil/app", "anything"))
    with pytest.raises(cc.CloudError, match="prompt"):
        _run(cc.dispatch("acme/app", ""))
    with pytest.raises(cc.CloudError, match="base_branch"):
        _run(cc.dispatch("acme/app", "x", base_branch="main; rm -rf /"))


def test_a_finished_run_reports_result_branch_and_pull_request(cloud):
    gh, events, _ = cloud
    record = _run(cc.dispatch("acme/app", "Fix the parser", owner="alice"))
    gh.run.update(status="in_progress")
    assert _run(cc.refresh(record["task_id"]))["status"] == "running"
    gh.run.update(status="completed", conclusion="success")
    gh.pr = {"html_url": "https://github.com/acme/app/pull/9", "number": 9}
    done = _run(cc.refresh(record["task_id"]))
    assert done["status"] == "completed"
    report = cc.report(done)
    assert report["exit_code"] == 0 and report["pr_url"].endswith("/pull/9")
    assert report["result"] == "Fixed the parser and added a test."
    assert report["branch"] == record["branch"]
    finished = [e for e in events if e[0] == "finished"][0]
    assert finished[2]["status"] == "completed" and finished[2]["data"]["pr_url"].endswith("/pull/9")


def test_no_changes_means_no_branch_and_says_so(cloud):
    gh, _, _ = cloud
    gh.branch_exists = False
    record = _run(cc.dispatch("acme/app", "Just review it"))
    gh.run.update(status="completed", conclusion="success")
    report = cc.report(_run(cc.refresh(record["task_id"])))
    assert report["branch"] is None and "no file changes" in report["note"]


def test_a_failed_run_is_reported_as_failed(cloud):
    gh, _, _ = cloud
    record = _run(cc.dispatch("acme/app", "Fix it"))
    gh.run.update(status="completed", conclusion="failure")
    report = cc.report(_run(cc.refresh(record["task_id"])))
    assert report["status"] == "failed" and report["exit_code"] == 1


def test_cancel_stops_the_run(cloud):
    gh, _, _ = cloud
    record = _run(cc.dispatch("acme/app", "Fix it"))
    gh.run.update(status="in_progress")
    assert _run(cc.cancel(record["task_id"]))["status"] == "cancelled"
    assert any(c[0] == "POST" and c[1].endswith("/runs/77/cancel") for c in gh.calls)


def test_tasks_are_owner_scoped_and_persisted(cloud, tmp_path):
    record = _run(cc.dispatch("acme/app", "Fix it", owner="alice"))
    assert cc.get(record["task_id"], owner="bob") is None
    saved = json.loads((tmp_path / "claude_cloud" / "tasks.json").read_text(encoding="utf-8"))
    assert saved[0]["task_id"] == record["task_id"]


def test_the_delegation_tool_routes_cloud_repositories_and_tasks(cloud, monkeypatch):
    from src.agent_tools import claude_code_tools as cct

    monkeypatch.setattr(cct, "_setting", lambda key, default=None: default)
    assert cct._wants_cloud({"repository": "acme/app"})
    assert cct._wants_cloud({"via": "cloud"})
    assert not cct._wants_cloud({"via": "local", "repository": "acme/app"})
    assert not cct._wants_cloud({"repository": "/app/data/development/odysseus"})

    out = _run(cct.ClaudeCodeTool().execute(json.dumps({"action": "start", "repository": "acme/app",
                                                         "prompt": "Fix it"}), {"owner": "alice"}))
    assert out["backend"] == "cloud" and out["task_id"].startswith("cloud-") and out["exit_code"] == 0
    polled = _run(cct.ClaudeCodeTool().execute(json.dumps({"action": "poll", "task_id": out["task_id"]}),
                                               {"owner": "alice"}))
    assert polled["task_id"] == out["task_id"] and polled["backend"] == "cloud"


def test_the_workflow_never_interpolates_dispatch_inputs_into_shell():
    """Dispatch inputs are untrusted text; ${{ }} inside a run: script is
    script injection. They must reach shell steps only via env."""
    import yaml

    wf = yaml.safe_load((ROOT / "clients/claude/github/odysseus-claude.yml").read_text(encoding="utf-8"))
    steps = wf["jobs"]["claude"]["steps"]
    for step in steps:
        assert "${{" not in str(step.get("run") or ""), step.get("name")
    claude = next(s for s in steps if s.get("uses", "").startswith("anthropics/claude-code-action@"))
    # Every action is pinned to a full commit SHA: the hub holds the Claude
    # credential and a GitHub App key, so a moved tag must not run with them.
    for step in steps:
        if step.get("uses"):
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
    # Claude runs in the checkout; the token must not be left in .git/config.
    checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] is False
    assert claude["with"]["prompt"] == "${{ inputs.prompt }}"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in claude["with"]["claude_code_oauth_token"]
    # Claude gets no push/PR ability; the workflow does the git plumbing.
    assert "git push" not in claude["with"]["claude_args"] and "gh " not in claude["with"]["claude_args"]
    on = wf.get("on") or wf.get(True)
    assert list(on) == ["workflow_dispatch"]


# ── hub mode: one workflow serves every repository ───────────────────────
def test_hub_mode_dispatches_in_the_hub_for_the_target_repository(cloud):
    """2026-09-28: every repository showed "not set up" because each needed
    its own workflow copy and secret. With a hub, only the hub has them."""
    gh, _, settings = cloud
    settings["claude_cloud_hub_repository"] = "acme/hub"
    record = _run(cc.dispatch("acme/app", "Fix the parser"))
    method, path, _, body = [c for c in gh.calls if c[0] == "POST"][0]
    assert path == "/repos/acme/hub/actions/workflows/odysseus-claude.yml/dispatches"
    assert body["ref"] == "dev"  # the hub's default branch
    # The target and its branch are spelled out: the hub checks it out itself.
    assert body["inputs"]["repository"] == "acme/app"
    assert body["inputs"]["base_branch"] == "main"
    assert record["repository"] == "acme/app" and record["workflow_repository"] == "acme/hub"

    # The run and its artifact are read from the hub, the PR from the target.
    gh.run.update(status="completed", conclusion="success")
    gh.pr = {"html_url": "https://github.com/acme/app/pull/3", "number": 3}
    done = _run(cc.refresh(record["task_id"]))
    assert done["status"] == "completed" and done["pr_url"].endswith("/acme/app/pull/3")
    assert done["result"] == "Fixed the parser and added a test."
    run_paths = [c[1] for c in gh.calls if c[1].endswith("/runs") or "/artifacts" in c[1]]
    assert run_paths and all(p.startswith("/repos/acme/hub/") for p in run_paths)
    assert ("GET", "/repos/acme/app/pulls") == [c[:2] for c in gh.calls if c[1].endswith("/pulls")][0]


def test_per_repository_dispatch_never_sends_the_repository_input(cloud):
    """A per-repository workflow without the `repository` input rejects a
    dispatch that carries it (HTTP 422), so only hub dispatches send it."""
    gh, _, _ = cloud
    _run(cc.dispatch("acme/app", "Fix it"))
    body = [c for c in gh.calls if c[0] == "POST"][0][3]
    assert "repository" not in body["inputs"]


def test_star_allows_any_repository_and_cancel_goes_to_the_hub(cloud):
    gh, _, settings = cloud
    settings["claude_cloud_repositories"] = ["*"]
    settings["claude_cloud_hub_repository"] = "https://github.com/acme/hub.git"
    assert cc.allows_any_repository() and cc.configured() and cc.repositories() == []
    assert cc.hub_repository() == "acme/hub"
    assert cc.is_cloud_repository("zeta/new")
    assert not cc.is_cloud_repository("/app/data/development/dog-trainer")
    assert not cc.is_cloud_repository("not a slug")
    record = _run(cc.dispatch("zeta/new", "Add a check-in form"))
    body = [c for c in gh.calls if c[0] == "POST"][0][3]
    assert body["inputs"]["repository"] == "zeta/new" and body["inputs"]["base_branch"] == "trunk"
    gh.run.update(status="in_progress")
    _run(cc.cancel(record["task_id"]))
    assert any(c[0] == "POST" and c[1] == "/repos/acme/hub/actions/runs/77/cancel" for c in gh.calls)


def test_status_in_hub_mode_checks_the_hub_workflow_and_target_visibility(cloud, monkeypatch):
    gh, _, settings = cloud
    settings["claude_cloud_repositories"] = ["acme/app", "*"]
    settings["claude_cloud_hub_repository"] = "acme/hub"

    class Cfg:
        has_any_credential = True
        has_github_app = True
        private_key_path = __file__
        fallback_token_env = "ODYSSEUS_AGENT_GITHUB_TOKEN"

    monkeypatch.setattr(cc, "_github_config", lambda: Cfg())
    out = _run(cc.status())
    assert out["ready"] is True and out["any_repository"] is True
    assert out["hub"] == {"repository": "acme/hub", "workflow": True}
    assert out["repositories"] == [{"repository": "acme/app", "via_hub": True, "workflow": True}]
    # Only the hub is asked for the workflow; targets only need to be visible.
    workflow_checks = [c[1] for c in gh.calls if "/actions/workflows/" in c[1]]
    assert workflow_checks == ["/repos/acme/hub/actions/workflows/odysseus-claude.yml"]

    settings["claude_cloud_hub_repository"] = ""
    settings["claude_cloud_repositories"] = ["*"]
    out = _run(cc.status())
    assert out["ready"] is False and any("hub" in h for h in out["hints"])


def test_the_tool_defaults_a_cloud_run_to_the_workspace_origin(cloud, monkeypatch, tmp_path):
    """With * and no repository named, "run it in the cloud" works on the
    repository the chat's workspace is a checkout of."""
    from src.agent_tools import claude_code_tools as cct
    from src.agent_worktree import service

    gh, _, settings = cloud
    settings["claude_cloud_repositories"] = ["*"]
    settings["claude_cloud_hub_repository"] = "acme/hub"
    monkeypatch.setattr(cct, "_setting", lambda key, default=None: default)
    monkeypatch.setattr("src.tool_execution.get_active_workspace", lambda: str(tmp_path))
    monkeypatch.setattr(service, "_origin_slug", lambda path: "zeta/new" if path == str(tmp_path) else "")
    out = _run(cct.ClaudeCodeTool().execute(json.dumps({"action": "start", "via": "cloud",
                                                         "prompt": "Fix it"}), {"owner": "alice"}))
    assert out["exit_code"] == 0 and out["repository"] == "zeta/new", out
    assert cct._wants_cloud({"repository": "zeta/new"})
    assert not cct._wants_cloud({"repository": "/app/data/development/dog-trainer"})


def test_a_public_hub_never_runs_a_private_repository(cloud, monkeypatch):
    """A public repository's Actions logs are public; the run would expose
    the private target's code and the prompt."""
    gh, _, settings = cloud
    settings["claude_cloud_repositories"] = ["*"]
    settings["claude_cloud_hub_repository"] = "acme/public-hub"
    with pytest.raises(cc.CloudError, match="public"):
        _run(cc.dispatch("zeta/secret", "Fix it"))
    assert not [c for c in gh.calls if c[0] == "POST"]
    _run(cc.dispatch("zeta/new", "Fix it"))  # a public target is fine

    class Cfg:
        has_any_credential = True
        has_github_app = True
        private_key_path = __file__
        fallback_token_env = "ODYSSEUS_AGENT_GITHUB_TOKEN"

    monkeypatch.setattr(cc, "_github_config", lambda: Cfg())
    out = _run(cc.status())
    assert out["hub"]["public"] is True and any("public" in h for h in out["hints"])
