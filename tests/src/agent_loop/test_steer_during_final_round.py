"""A message sent while the last round runs is read, not cancelled.

A mid-turn message is queued as a steer and drained at the top of a round.
If it lands while the final round is already running, nothing drains it
again: the turn ends, the queue is cleared and the message vanishes
(reported 2026-09-20). The loop runs one more round for it, a bounded number
of times, and only for a steer queued by this run.
"""
import asyncio
import json

import pytest

import src.agent_loop as al
from src import agent_activity, agent_control, constants


@pytest.fixture(autouse=True)
def _isolated_steer_queue(tmp_path, monkeypatch):
    # Queueing a steer writes to the activity feed, so relocate the data dir.
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    agent_activity._reset_for_tests()
    agent_control._STEER.clear()
    yield
    agent_control._STEER.clear()
    agent_activity._reset_for_tests()


def _run(monkeypatch, steer_during_round):
    """Run a tool-free turn. `steer_during_round(n)` queues steers while round n streams.

    Returns what the model saw in each round and the frames the client got.
    """
    seen = []

    async def fake_model(_candidates, messages, **kwargs):
        seen.append([dict(m) for m in messages])
        steer_during_round(len(seen))
        yield f'data: {json.dumps({"delta": f"answer {len(seen)}"})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_model)
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None)

    async def run():
        loop = al.stream_agent_loop(
            "http://model.test/v1", "m",
            [{"role": "user", "content": "Summarise the quarterly report in my shared folder and list the three biggest risks it names, then compare them with last year."}],
            session_id="s", steer_run_id="r1",
        )
        return [chunk async for chunk in loop]

    frames = asyncio.run(run())
    return seen, frames


def _mentions(messages, text):
    return any(text in str(m.get("content", "")) for m in messages)


def test_a_steer_that_lands_in_the_final_round_is_read_in_another_round(monkeypatch):
    def steer(n):
        if n == 1:
            agent_control.steer("s", "also check the archive", run_id="r1")

    seen, frames = _run(monkeypatch, steer)

    assert len(seen) == 2
    assert _mentions(seen[1], "also check the archive")
    assert not any("steer_dropped" in f for f in frames)


def test_a_client_that_keeps_steering_cannot_hold_the_run_open(monkeypatch):
    def steer(n):
        if n < 20:  # a loop that never stops extending still ends the test
            agent_control.steer("s", f"message {n}", run_id="r1")

    seen, frames = _run(monkeypatch, steer)

    assert 2 < len(seen) < 20
    dropped = [f for f in frames if "steer_dropped" in f]
    assert dropped and f"message {len(seen)}" in dropped[0]


def test_a_steer_queued_for_another_run_does_not_extend_this_turn(monkeypatch):
    def steer(n):
        if n == 1:
            agent_control.steer("s", "for the sibling run", run_id="other")

    seen, _frames = _run(monkeypatch, steer)

    assert len(seen) == 1
