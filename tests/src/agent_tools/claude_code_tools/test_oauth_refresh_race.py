"""A delegation that loses the shared OAuth token refresh to another Claude Code
process is retried once by the tool, not handed back to the agent as a failure.

2026-10-07: two delegations started close together; the second exited in 8 s
with "another Claude Code process is refreshing it", and the worker spent a
round and a relaunch of its own on an error the CLI calls transient.
"""
import asyncio

from src.agent_tools import claude_code_tools as cct

_RACE = ("Failed to refresh OAuth token: another Claude Code process is refreshing it or exited "
         "mid-refresh. This is usually transient; retry in a minute")


def _run(monkeypatch, tmp_path, results):
    calls = []

    async def fake_run(repository, prompt, timeout, tools, **kwargs):
        calls.append(prompt)
        return dict(results[len(calls) - 1])

    monkeypatch.setattr(cct, "_run_claude", fake_run)
    monkeypatch.setattr(cct, "_OAUTH_RACE_RETRY_S", 0, raising=False)
    result = asyncio.run(cct._run_with_auto_update(tmp_path, "fix it", 30, ["Read"]))
    return result, calls


def test_a_lost_token_refresh_is_retried_once(monkeypatch, tmp_path):
    result, calls = _run(monkeypatch, tmp_path, [
        {"error": _RACE, "exit_code": 1},
        {"output": "fixed", "exit_code": 0},
    ])

    assert len(calls) == 2
    assert result["exit_code"] == 0


def test_a_run_that_changed_the_checkout_is_not_rerun(monkeypatch, tmp_path):
    result, calls = _run(monkeypatch, tmp_path, [
        {"error": _RACE, "exit_code": 1, "changes": ["src/a.py"]},
    ])

    assert len(calls) == 1
    assert result["error"] == _RACE
