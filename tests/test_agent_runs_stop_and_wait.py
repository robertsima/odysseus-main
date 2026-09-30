"""A message that replaces a running turn is saved after that turn's partial reply.

agent_runs.start cancels the old run, but the new message was already in the
history by then, so the stopped turn's text landed below "did you get stuck?"
(2026-09-29). The route now calls stop_and_wait before saving the message.
"""
import asyncio

import pytest

from src import agent_runs


@pytest.mark.asyncio
async def test_stop_and_wait_returns_after_the_partial_is_saved():
    saved = []

    async def turn():
        try:
            yield "data: {\"delta\": \"working\"}\n\n"
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)  # the partial save does I/O
            saved.append("partial")
            raise

    agent_runs.start("s-replace", turn())
    await asyncio.sleep(0.05)
    assert agent_runs.is_active("s-replace")

    assert await agent_runs.stop_and_wait("s-replace") is True
    assert saved == ["partial"]
    assert not agent_runs.is_active("s-replace")
    assert await agent_runs.stop_and_wait("s-replace") is False
    agent_runs._RUNS.pop("s-replace", None)


def test_route_stops_the_running_turn_before_saving_the_message():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "routes" / "chat_routes.py").read_text(encoding="utf-8")
    handler = src[src.index("async def chat_stream"):]
    assert handler.index("agent_runs.stop_and_wait(session)") < handler.index("ctx = await build_chat_context(")
