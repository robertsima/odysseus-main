"""manage_agent_worktree checks: grouped CI results, base comparison, waiting.

Incident 2026-10-02: workers polled a PR's CI with `bash sleep 60` between
model rounds (27 calls in an hour), and spent rounds on failures the branch
did not cause (hadolint on an untouched Dockerfile, a dependency-review job
that fails on every PR). One call now waits in the harness and marks those.
"""
import json

import pytest

from src import agent_control
from src.agent_tools.worktree_tools import AgentWorktreeTool
from src.agent_worktree import checks as checks_mod
from src.agent_worktree import github as gh
from src.agent_worktree import service
from src.agent_worktree.config import WorktreeConfig

HEAD = "a" * 40
BASE = "b" * 40
BRANCH = "agent/odysseus/fix"


def _cfg():
    return WorktreeConfig(
        publish_enabled=True, repo_slug="acme/odysseus", source_repo="/tmp/src", worktree_root="/tmp/wt",
        state_dir="/tmp/state", base_branch="dev", approval_ttl_s=900, api_base="https://api.github.com",
        app_id="1", installation_id="2", private_key_path="/nope.pem",
        fallback_token_env="ODYSSEUS_AGENT_GITHUB_TOKEN", _fallback_token_present=False,
    )


def _run(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion, "url": f"https://ci/{name}",
            "app": "gha", "started_at": None, "completed_at": None}


class FakeGitHub:
    """Check runs per commit; head_polls gives the head's runs for each successive poll."""

    def __init__(self):
        self.head_polls = [[_run("tests")]]
        self.base_runs = []
        self.pr = {"number": 7, "url": "https://github.com/acme/odysseus/pull/7", "draft": True}
        self.calls = []

    async def resolve_token(self, cfg):
        return "tok"

    async def find_open_pr(self, cfg, token, branch):
        self.calls.append(("find", branch))
        return self.pr and {"number": self.pr["number"]}

    async def get_pull_request(self, cfg, token, number):
        return {**self.pr, "head": {"sha": HEAD}, "base": {"ref": "dev"}}

    async def pull_request_checks(self, cfg, token, sha):
        self.calls.append(("checks", sha))
        if sha == BASE:
            return self.base_runs
        polls = [c for c in self.calls if c == ("checks", HEAD)]
        return self.head_polls[min(len(polls), len(self.head_polls)) - 1]

    async def branch_head_sha(self, cfg, token, branch):
        self.calls.append(("branch", branch))
        return BASE


@pytest.fixture
def fake(monkeypatch):
    f = FakeGitHub()
    for name in ("resolve_token", "find_open_pr", "get_pull_request", "pull_request_checks", "branch_head_sha"):
        monkeypatch.setattr(gh, name, getattr(f, name))
    monkeypatch.setattr(gh, "pr_access_blockers", lambda cfg: [])

    async def locate(cfg, name, repository):
        return _cfg(), BRANCH, "/tmp/wt/fix"

    monkeypatch.setattr(service, "_locate", locate)
    monkeypatch.setattr("src.agent_worktree.config.load_config", _cfg)
    return f


async def _checks(**args):
    return await AgentWorktreeTool().execute(json.dumps({"action": "checks", "branch": BRANCH, **args}), {})


async def test_groups_checks_and_marks_failures_the_base_also_has(fake):
    fake.head_polls = [[
        _run("tests"), _run("lint"), _run("hadolint", conclusion="failure"),
        _run("pytest", conclusion="timed_out"), _run("docs", conclusion="skipped"),
        _run("build", status="in_progress", conclusion=None),
    ]]
    fake.base_runs = [_run("hadolint", conclusion="failure"), _run("pytest")]
    out = await _checks()
    assert out["exit_code"] == 0
    assert out["pull_request"]["number"] == 7 and out["head_sha"] == HEAD
    assert out["counts"] == {"in_progress": 1, "passed": 2, "failed": 2, "skipped": 1}
    failed = {f["name"]: f["also_fails_on_base"] for f in out["failed"]}
    assert failed == {"hadolint": True, "pytest": False}
    assert out["passed"] == ["tests", "lint"] and out["skipped"] == ["docs"]
    assert out["in_progress"][0]["name"] == "build"
    assert out["complete"] is False
    assert "Introduced by this branch: pytest" in out["summary"]
    assert "Already failing on dev: hadolint" in out["summary"]
    # one base lookup, however many failures
    assert [c for c in fake.calls if c[0] == "branch"] == [("branch", "dev")]
    assert out["base"] == {"ref": "dev", "sha": BASE, "checked": True}


async def test_base_is_not_read_when_nothing_failed(fake):
    out = await _checks()
    assert out["complete"] is True and out["failed"] == []
    assert not [c for c in fake.calls if c[0] == "branch"]
    assert "base" not in out


async def test_missing_base_result_is_handled(fake, monkeypatch):
    fake.head_polls = [[_run("hadolint", conclusion="failure")]]

    async def boom(cfg, token, branch):
        raise gh.GitHubError("GitHub GET /branches failed (404)")

    monkeypatch.setattr(gh, "branch_head_sha", boom)
    out = await _checks()
    assert out["failed"][0]["also_fails_on_base"] is False
    assert out["base"]["checked"] is False
    assert "could not be read" in out["summary"]


async def test_wait_polls_until_checks_finish(fake, monkeypatch):
    running = [_run("tests", status="in_progress", conclusion=None)]
    fake.head_polls = [running, running, [_run("tests")]]
    sleeps = []

    async def fake_wait(session_id, timeout, *aw):
        sleeps.append(timeout)
        return False

    monkeypatch.setattr(agent_control, "wait_or_steer", fake_wait)
    out = await _checks(wait_seconds=600)
    assert out["complete"] is True and out["passed"] == ["tests"]
    assert len(sleeps) == 2 and all(20 <= s <= 30 for s in sleeps)
    assert "steer_note" not in out


async def test_wait_stops_at_the_time_limit(fake, monkeypatch):
    fake.head_polls = [[_run("tests", status="queued", conclusion=None)]]
    clock = [1000.0]
    monkeypatch.setattr(checks_mod, "_monotonic", lambda: clock[0])

    async def fake_wait(session_id, timeout, *aw):
        clock[0] += timeout
        return False

    monkeypatch.setattr(agent_control, "wait_or_steer", fake_wait)
    out = await _checks(wait_seconds=60)
    assert out["complete"] is False and out["waited_s"] == 60.0
    assert out["counts"]["in_progress"] == 1


async def test_pending_steer_ends_the_wait_with_the_note(fake, monkeypatch):
    fake.head_polls = [[_run("tests", status="in_progress", conclusion=None)]]
    calls = []

    async def fake_wait(session_id, timeout, *aw):
        calls.append(session_id)
        return True

    monkeypatch.setattr(agent_control, "wait_or_steer", fake_wait)
    out = await AgentWorktreeTool().execute(
        json.dumps({"action": "checks", "branch": BRANCH, "wait_seconds": 600}), {"session_id": "s1"})
    assert calls == ["s1"]
    assert out["complete"] is False and out["steer_note"] == agent_control.STEER_WAIT_NOTE


async def test_no_pull_request_says_what_to_do(fake):
    fake.pr = None
    out = await _checks()
    assert out["exit_code"] == 1 and out["code"] == "NO_OPEN_PULL_REQUEST"
    assert "request_publish" in out["error"] and "publish block" in out["error"]
    assert out["next_action"]["action"] == "status"


async def test_no_checks_yet_is_not_complete(fake):
    fake.head_polls = [[]]
    out = await _checks()
    assert out["complete"] is False and "No check runs" in out["summary"]


async def test_github_not_configured_is_reported(fake, monkeypatch):
    monkeypatch.setattr(gh, "pr_access_blockers", lambda cfg: ["no GitHub credential configured"])
    out = await _checks()
    assert out["code"] == "GITHUB_UNAVAILABLE" and "no GitHub credential" in out["error"]


async def test_branch_is_required():
    out = await AgentWorktreeTool().execute(json.dumps({"action": "checks"}), {})
    assert out["exit_code"] == 1 and out["code"] == "MISSING_BRANCH"


def test_checks_wait_is_a_wait_only_call_and_the_note_names_it():
    assert agent_control.is_wait_only_call(
        "manage_agent_worktree", json.dumps({"action": "checks", "wait_seconds": 300}))
    assert not agent_control.is_wait_only_call("manage_agent_worktree", json.dumps({"action": "checks"}))
    assert not agent_control.is_wait_only_call(
        "manage_agent_worktree", json.dumps({"action": "commit", "wait_seconds": 5}))
    note = agent_control._PUBLISH_FOLLOWUP_NOTE
    assert "manage_agent_worktree checks" in note and "wait_seconds" in note and "{budget}" in note
    assert "also_fails_on_base" in note and "sleep" not in note


async def test_branch_head_sha_reads_the_branches_endpoint(monkeypatch):
    seen = []

    async def fake_api(cfg, token, method, path, **kw):
        seen.append(path)
        return {"commit": {"sha": BASE}}

    monkeypatch.setattr(gh, "_api", fake_api)
    assert await gh.branch_head_sha(_cfg(), "tok", "release/1.0") == BASE
    assert seen == ["/repos/acme/odysseus/branches/release/1.0"]
