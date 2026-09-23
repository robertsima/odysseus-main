"""Claude Code cloud runner: delegation to Claude Code in GitHub Actions.

Odysseus dispatches a workflow in an allowlisted repository and follows it;
Claude's credential lives only in that repository's secrets.
"""
import asyncio
import io
import json
import zipfile
from pathlib import Path

import pytest

from src import claude_cloud as cc

ROOT = Path(__file__).resolve().parent.parent


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

    wf = yaml.safe_load((ROOT / "integrations/claude/github/odysseus-claude.yml").read_text(encoding="utf-8"))
    steps = wf["jobs"]["claude"]["steps"]
    for step in steps:
        assert "${{" not in str(step.get("run") or ""), step.get("name")
    claude = next(s for s in steps if s.get("uses", "").startswith("anthropics/claude-code-action@"))
    assert claude["uses"].endswith("@v1")
    assert claude["with"]["prompt"] == "${{ inputs.prompt }}"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in claude["with"]["claude_code_oauth_token"]
    # Claude gets no push/PR ability; the workflow does the git plumbing.
    assert "git push" not in claude["with"]["claude_args"] and "gh " not in claude["with"]["claude_args"]
    on = wf.get("on") or wf.get(True)
    assert list(on) == ["workflow_dispatch"]
