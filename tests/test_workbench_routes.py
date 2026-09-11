"""Workbench router: admin-gated, and each surface answers from the module
behind it (activity feed, repo inspection, PR client)."""
import os
import subprocess
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routes import workbench_routes as wr
from src import agent_activity as act
from src import constants
from src import repo_inspect


def _endpoints():
    router = wr.setup_workbench_routes()
    out = {}
    for route in router.routes:
        for method in route.methods:
            out[(method, route.path)] = route.endpoint
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "data"))
    act._reset_for_tests()
    monkeypatch.setattr(wr, "_admin", lambda request: "alice")
    yield tmp_path
    act._reset_for_tests()


def _req():
    return SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace()))


def test_every_route_is_admin_gated(monkeypatch):
    calls = []

    def deny(request):
        calls.append(1)
        raise HTTPException(403, "Admin only")

    monkeypatch.setattr(wr, "require_admin", deny)
    monkeypatch.setattr(wr, "require_user", lambda request: "mallory")
    with pytest.raises(HTTPException) as exc:
        wr._admin(_req())
    assert exc.value.status_code == 403 and calls


async def test_activity_and_runs_endpoints(env):
    ep = _endpoints()
    rid = act.run_started("s1", "claude_code", "Claude Code: x", owner="alice")
    act.publish("s1", "tool_start", "Edit a.py", source="claude_code", run_id=rid)
    hist = await ep[("GET", "/api/workbench/activity")](_req(), session_id="s1", since=0, limit=50)
    assert [e["kind"] for e in hist["events"]] == ["run_started", "tool_start"] and hist["seq"] == 2
    later = await ep[("GET", "/api/workbench/activity")](_req(), session_id="s1", since=1, limit=50)
    assert [e["kind"] for e in later["events"]] == ["tool_start"]
    runs = await ep[("GET", "/api/workbench/runs")](_req(), session_id=None, limit=10, active=True)
    assert runs["runs"][0]["run_id"] == rid
    detail = await ep[("GET", "/api/workbench/runs/{run_id}")](_req(), run_id=rid)
    assert detail["status"] == "running" and len(detail["events"]) == 2
    with pytest.raises(HTTPException) as exc:
        await ep[("GET", "/api/workbench/runs/{run_id}")](_req(), run_id="nope")
    assert exc.value.status_code == 404
    stream = await ep[("GET", "/api/workbench/activity/stream")](_req(), session_id="s1", since=0)
    assert stream.media_type == "text/event-stream"


async def test_repo_endpoints_confine_and_answer(env, monkeypatch):
    repo = env / "roots" / "proj"
    repo.mkdir(parents=True)
    genv = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=genv)
    (repo / "f.txt").write_text("one\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=genv)
    subprocess.run(["git", "commit", "-q", "-m", "first"], cwd=repo, check=True, env=genv)
    (repo / "f.txt").write_text("one\ntwo\n")
    monkeypatch.setattr(repo_inspect, "allowed_roots", lambda: [str(env / "roots")])
    ep = _endpoints()

    st = await ep[("GET", "/api/workbench/repo/status")](_req(), path=str(repo))
    assert st["branch"] == "main" and st["dirty"] is True
    ch = await ep[("GET", "/api/workbench/repo/changes")](_req(), path=str(repo), base=None)
    assert ch["files"][0]["path"] == "f.txt" and ch["files"][0]["additions"] == 1
    df = await ep[("GET", "/api/workbench/repo/diff")](_req(), path=str(repo), file="f.txt", base=None, commit=None)
    assert "+two" in df["diff"]
    log = await ep[("GET", "/api/workbench/repo/commits")](_req(), path=str(repo), limit=5, base=None)
    assert log["commits"][0]["subject"] == "first"
    with pytest.raises(HTTPException) as exc:
        await ep[("GET", "/api/workbench/repo/status")](_req(), path="/etc")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        await ep[("GET", "/api/workbench/repo/diff")](_req(), path=str(repo), file="../../etc/passwd", base=None, commit=None)


async def test_pr_endpoints_report_blockers_and_post_feedback(env, monkeypatch):
    ep = _endpoints()
    cfg = SimpleNamespace(repo_slug="acme/odysseus", base_branch="dev", has_github_app=False, has_any_credential=False)
    monkeypatch.setattr(wr, "_pr_context", lambda: (cfg, ["no GitHub credential configured"]))
    conf = await ep[("GET", "/api/workbench/prs/config")](_req())
    assert conf["configured"] is False and conf["blockers"]
    with pytest.raises(HTTPException) as exc:
        await ep[("GET", "/api/workbench/prs")](_req(), state="open", limit=5)
    assert exc.value.status_code == 409

    monkeypatch.setattr(wr, "_pr_context", lambda: (cfg, []))

    async def fake_token(c):
        return "tok"

    monkeypatch.setattr(wr, "_pr_token", fake_token)
    from src.agent_worktree import github as gh
    posted = {}

    async def fake_comment(c, token, number, body):
        posted["comment"] = (number, body)
        return {"id": 5, "url": "https://github.com/acme/odysseus/pull/3#c5"}

    async def fake_review(c, token, number, *, event, body="", comments=None):
        posted["review"] = (number, event, body, comments)
        return {"id": 9, "state": "CHANGES_REQUESTED", "url": "u"}

    async def fake_list(c, token, *, state="open", limit=30):
        return [{"number": 3, "title": "t"}]

    monkeypatch.setattr(gh, "create_issue_comment", fake_comment)
    monkeypatch.setattr(gh, "create_review", fake_review)
    monkeypatch.setattr(gh, "list_pull_requests", fake_list)

    listed = await ep[("GET", "/api/workbench/prs")](_req(), state="open", limit=5)
    assert listed["pulls"][0]["number"] == 3
    out = await ep[("POST", "/api/workbench/prs/{number}/comment")](_req(), number=3, body={"body": "looks good", "session_id": "s9"})
    assert out["id"] == 5 and posted["comment"] == (3, "looks good")
    out = await ep[("POST", "/api/workbench/prs/{number}/review")](
        _req(), number=3, body={"event": "REQUEST_CHANGES", "body": "nope", "comments": [{"path": "a", "line": 1, "body": "x"}], "session_id": "s9"})
    assert out["state"] == "CHANGES_REQUESTED" and posted["review"][1] == "REQUEST_CHANGES"
    notes = [e for e in act.history("s9") if e["kind"] == "note"]
    assert len(notes) == 2 and notes[0]["source"] == "worktree" and notes[0]["data"]["pull_request"] == 3
