"""Stop on a worker's or sub-agent's chat stops that work.

Workers, sub-agents (a loadout's child chat) and background follow-ups run
without a chat stream, so the chat Stop found nothing to cancel: on
2026-09-29 five Stops on a Planning Command Center worker's chat all answered
"Nothing to stop" while it kept running.
"""
import pytest

from src import agent_control


RUNS = [
    {"run_id": "worker-1", "source": "session", "status": "running", "session_id": "worker-chat",
     "summary": {"parent_session": "parent-chat"}},
    {"run_id": "sub-1", "source": "session", "status": "running", "session_id": "parent-chat",
     "summary": {"target_session": "child-chat"}},
    {"run_id": "turn-1", "source": "odysseus", "status": "running", "session_id": "worker-chat",
     "summary": {}},
    {"run_id": "old-1", "source": "session", "status": "completed", "session_id": "worker-chat",
     "summary": {}},
]


@pytest.fixture
def stopped(monkeypatch):
    calls = []
    monkeypatch.setattr(agent_control.activity, "list_runs", lambda **_k: [dict(r) for r in RUNS])

    async def fake_stop_run(run_id, *, by="by the user"):
        calls.append((run_id, by))
        return {"stopped": True, "how": "headless"}

    monkeypatch.setattr(agent_control, "stop_run", fake_stop_run)
    return calls


@pytest.mark.asyncio
async def test_stop_reaches_a_worker_running_in_its_own_chat(stopped):
    assert await agent_control.stop_chat_work("worker-chat") == 1
    assert [run for run, _ in stopped] == ["worker-1"]
    assert stopped[0][1] == "from the chat's Stop button"


@pytest.mark.asyncio
async def test_stop_reaches_a_sub_agent_working_in_a_child_chat(stopped):
    assert await agent_control.stop_chat_work("child-chat") == 1
    assert [run for run, _ in stopped] == ["sub-1"]


@pytest.mark.asyncio
async def test_nothing_running_in_the_chat_stops_nothing(stopped):
    assert await agent_control.stop_chat_work("quiet-chat") == 0
    assert stopped == []


@pytest.mark.asyncio
async def test_a_run_that_cannot_be_stopped_is_skipped(monkeypatch):
    monkeypatch.setattr(agent_control.activity, "list_runs", lambda **_k: [dict(RUNS[0])])

    async def refuse(run_id, *, by="by the user"):
        raise ValueError("not stoppable")

    monkeypatch.setattr(agent_control, "stop_run", refuse)
    assert await agent_control.stop_chat_work("worker-chat") == 0
