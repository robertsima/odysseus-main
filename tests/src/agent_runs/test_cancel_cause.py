"""A cancelled turn knows why it was cancelled.

2026-10-10: the chat route logged "Client disconnected mid-stream" for every
cancelled turn. Turns are detached and survive a closed tab, so the real
cancellers are a newer message in the chat, Stop and shutdown; the log named
none of them and sent the investigation to the browser.
"""
import asyncio

import pytest

from src import agent_runs


async def _seen_cause_when_cancelled(session_id, how):
    seen = {}

    async def turn():
        try:
            yield "data: {\"delta\": \"working\"}\n\n"
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # What the chat route's cancel handler reads before it logs.
            seen["cause"] = agent_runs.cancel_cause()
            raise

    run = agent_runs.start(session_id, turn())
    await asyncio.sleep(0.05)
    await how(session_id, turn)
    await asyncio.sleep(0.05)
    return seen.get("cause"), run


@pytest.mark.asyncio
async def test_a_new_message_in_the_chat_is_named_as_the_cause():
    async def replace(sid, turn):
        assert await agent_runs.stop_and_wait(sid)

    cause, run = await _seen_cause_when_cancelled("s-cause-replace", replace)
    assert "new message" in cause
    assert run.cancel_cause == "replaced"
    agent_runs._RUNS.pop("s-cause-replace", None)


@pytest.mark.asyncio
async def test_a_second_run_started_over_the_first_is_named_as_the_cause():
    async def replace(sid, turn):
        async def second():
            yield "data: [DONE]\n\n"

        agent_runs.start(sid, second())

    cause, _run = await _seen_cause_when_cancelled("s-cause-start", replace)
    assert "new message" in cause
    agent_runs._RUNS.pop("s-cause-start", None)


@pytest.mark.asyncio
async def test_stop_and_shutdown_are_told_apart():
    async def stop(sid, turn):
        assert agent_runs.stop(sid)

    async def shutdown(sid, turn):
        assert await agent_runs.stop_all(timeout=1) == 1

    stopped, _ = await _seen_cause_when_cancelled("s-cause-stop", stop)
    shut, _ = await _seen_cause_when_cancelled("s-cause-shutdown", shutdown)
    assert "Stop" in stopped
    assert "shutting down" in shut
    for sid in ("s-cause-stop", "s-cause-shutdown"):
        agent_runs._RUNS.pop(sid, None)


@pytest.mark.asyncio
async def test_a_cancel_nobody_recorded_says_so_instead_of_blaming_the_client():
    async def bare_cancel(sid, turn):
        agent_runs._RUNS[sid].task.cancel()

    cause, _ = await _seen_cause_when_cancelled("s-cause-unknown", bare_cancel)
    assert "unknown" in cause and "disconnect" not in cause
    agent_runs._RUNS.pop("s-cause-unknown", None)
