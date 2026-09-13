"""Multi-chat run visibility, per-sub-agent stop, and sub-agent tool policy.

Before these changes only the browser tab that sent a message knew a chat was
running; a sub-agent could only be stopped by stopping its parent turn; and a
headless child skipped the operator's global disabled-tools setting.
"""
import asyncio
import json

import pytest

from src import agent_activity as act
from src import agent_runs, constants
from src import headless_agent


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    act._reset_for_tests()
    agent_runs._RUNS.clear()
    agent_runs._EXTERNAL.clear()
    agent_runs._FINISHED.clear()
    yield
    act._reset_for_tests()
    agent_runs._RUNS.clear()
    agent_runs._EXTERNAL.clear()
    agent_runs._FINISHED.clear()


def _sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


async def test_streamed_runs_are_listed_running_then_finished_and_scoped():
    release = asyncio.Event()

    async def gen():
        yield _sse({"delta": "hi"})
        await release.wait()
        yield "data: [DONE]\n\n"

    agent_runs.start("chat-a", gen(), owner="alice")
    await asyncio.sleep(0)
    rows = agent_runs.list_runs({"chat-a"})
    assert [(r["session_id"], r["status"]) for r in rows] == [("chat-a", "running")]
    assert agent_runs.list_runs({"someone-elses"}) == []

    release.set()
    await agent_runs._RUNS["chat-a"].task
    row = agent_runs.list_runs({"chat-a"})[0]
    assert row["status"] == "done" and row["finished_at"] >= row["started_at"]


def test_background_turns_mark_the_chat_busy():
    with agent_runs.track_external("chat-b", source="bg_job", owner="alice"):
        assert agent_runs.is_busy("chat-b") and not agent_runs.is_active("chat-b")
        assert agent_runs.list_runs({"chat-b"})[0]["status"] == "running"
    assert not agent_runs.is_busy("chat-b")
    assert agent_runs.list_runs({"chat-b"})[0]["status"] == "done"


class _Sess:
    id = "child"
    endpoint_url = "http://llm.test/v1"
    model = "m"
    headers = {}
    owner = "alice"
    context_length = 0


async def test_request_stop_ends_one_subagent_and_keeps_its_partial(monkeypatch):
    started = asyncio.Event()

    async def slow_loop(url, model, messages, **kwargs):
        yield _sse({"delta": "Checked a.py so far"})
        started.set()
        await asyncio.sleep(30)
        yield _sse({"delta": " and more"})

    import src.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "stream_agent_loop", slow_loop)
    outcome = {}
    task = asyncio.ensure_future(headless_agent.run_headless(
        _Sess(), [{"role": "user", "content": "go"}], run_id="session-abc",
        activity_session_id="parent", outcome=outcome))
    await started.wait()
    assert "session-abc" in headless_agent.running_ids()
    assert headless_agent.request_stop("session-abc") is True
    text, _events = await asyncio.wait_for(task, 5)
    assert outcome == {"stopped": True}
    assert text.startswith("Checked a.py so far") and "stopped by the user" in text
    assert "session-abc" not in headless_agent.running_ids()
    assert headless_agent.request_stop("session-abc") is False


async def test_headless_children_get_the_global_disabled_tools(monkeypatch):
    seen = {}

    async def fake_loop(url, model, messages, **kwargs):
        seen.update(kwargs)
        yield "data: [DONE]\n\n"

    import src.agent_loop as agent_loop
    import src.settings as settings
    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)
    monkeypatch.setattr(settings, "get_setting",
                        lambda key, default=None: ["bash", "web_fetch"] if key == "disabled_tools" else default)

    await headless_agent.run_headless(_Sess(), [], run_id=None)
    assert {"bash", "web_fetch", "send_to_session"} <= set(seen["disabled_tools"])

    # A chat continuing itself (disabled_tools=None) still honours the operator.
    await headless_agent.run_headless(_Sess(), [], disabled_tools=None)
    assert set(seen["disabled_tools"]) == {"bash", "web_fetch"}


def test_bg_job_is_a_run_from_launch_and_kill_closes_it(monkeypatch, tmp_path):
    from src import bg_jobs

    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(bg_jobs, "_kill", lambda pid: None)

    class _Proc:
        pid = 4242

    monkeypatch.setattr(bg_jobs.subprocess, "Popen", lambda *a, **k: _Proc())
    rec = bg_jobs.launch("sleep 100", "chat-c")
    run = act.get_run(f"bg_job-{rec['id']}")
    assert run["status"] == "running" and run["summary"]["job_id"] == rec["id"]
    bg_jobs.kill(rec["id"])
    assert act.get_run(f"bg_job-{rec['id']}")["status"] == "cancelled"
