"""checks: stop waiting once required checks finish, and report mergeable_state.

2026-10-07/08: a report-only job held a checks wait 31 minutes after every
required check had passed, and checks never said a PR was behind or
conflicted, so agents waited on CI for a PR that could not merge.
"""
import pytest

from src import agent_control
from src.agent_worktree import github as gh
from tests.src.agent_worktree.config.test_agent_worktree_checks import (  # noqa: F401 - fixture
    _checks, _cfg, _run, fake,
)

REPORT_ONLY = _run("red-evidence", status="in_progress", conclusion=None)


@pytest.fixture
def waits(monkeypatch):
    sleeps = []

    async def fake_wait(session_id, timeout, *aw):
        sleeps.append(timeout)
        return False

    monkeypatch.setattr(agent_control, "wait_or_steer", fake_wait)
    return sleeps


def _required(monkeypatch, names):
    async def required(cfg, token, branch):
        assert branch == "dev"
        return names

    monkeypatch.setattr(gh, "required_check_names", required)


async def test_wait_ends_when_required_checks_finish_and_lists_the_rest(fake, waits, monkeypatch):
    _required(monkeypatch, {"tests"})
    fake.head_polls = [[_run("tests", status="in_progress", conclusion=None), REPORT_ONLY],
                       [_run("tests"), REPORT_ONLY]]
    out = await _checks(wait_seconds=900)
    assert len(waits) == 1
    assert out["required_checks"] == ["tests"] and out["required_complete"] is True
    assert [r["name"] for r in out["non_blocking_in_progress"]] == ["red-evidence"]
    assert out["complete"] is False
    assert "do not block merging" in out["summary"]


async def test_unknown_required_checks_keep_waiting_for_everything(fake, waits, monkeypatch):
    _required(monkeypatch, None)
    fake.head_polls = [[_run("tests"), REPORT_ONLY], [_run("tests"), REPORT_ONLY],
                       [_run("tests"), _run("red-evidence")]]
    out = await _checks(wait_seconds=900)
    assert len(waits) == 2 and out["complete"] is True
    assert "required_checks" not in out and "non_blocking_in_progress" not in out


async def test_a_required_check_that_has_not_started_keeps_the_wait(fake, waits, monkeypatch):
    _required(monkeypatch, {"tests", "build"})
    fake.head_polls = [[_run("tests"), REPORT_ONLY], [_run("tests"), _run("build"), REPORT_ONLY]]
    out = await _checks(wait_seconds=900)
    assert len(waits) == 1 and out["required_complete"] is True


async def test_mergeable_state_is_reported_with_what_to_do(fake, monkeypatch):
    _required(monkeypatch, None)
    fake.pr = {**fake.pr, "mergeable_state": "behind"}
    out = await _checks()
    assert out["mergeable_state"] == "behind"
    assert "sync" in out["mergeable_hint"] and "sync" in out["summary"]


async def test_mergeable_state_is_read_again_when_github_had_not_computed_it(fake, monkeypatch):
    _required(monkeypatch, None)
    states = iter(["unknown", "dirty"])
    original = fake.get_pull_request

    async def get_pr(cfg, token, number):
        return {**(await original(cfg, token, number)), "mergeable_state": next(states)}

    monkeypatch.setattr(gh, "get_pull_request", get_pr)
    out = await _checks()
    assert out["mergeable_state"] == "dirty" and "conflicts" in out["mergeable_hint"]


async def test_required_check_names_come_from_rulesets_and_protection(monkeypatch):
    async def fake_api(cfg, token, method, path, **kw):
        if "/rules/branches/" in path:
            return [{"type": "required_status_checks",
                     "parameters": {"required_status_checks": [{"context": "tests"}]}},
                    {"type": "pull_request", "parameters": {}}]
        return {"contexts": ["lint"], "checks": [{"context": "lint"}, {"context": "build"}]}

    monkeypatch.setattr(gh, "_api", fake_api)
    assert await gh.required_check_names(_cfg(), "tok", "dev") == {"tests", "lint", "build"}


async def test_unreadable_protection_means_required_checks_are_unknown(monkeypatch):
    async def fake_api(cfg, token, method, path, **kw):
        if "/rules/branches/" in path:
            return [{"type": "required_status_checks",
                     "parameters": {"required_status_checks": [{"context": "tests"}]}}]
        err = gh.GitHubError("GitHub GET protection failed (403)")
        err.status = 403
        raise err

    monkeypatch.setattr(gh, "_api", fake_api)
    assert await gh.required_check_names(_cfg(), "tok", "dev") is None
