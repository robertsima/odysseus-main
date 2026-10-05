"""Compare panes stream directly; normal chats run detached.

A Compare pane's Stop closes its SSE, and that must cancel the upstream call.
Only a stream that is not wrapped in agent_runs is cancelled that way: a
detached run keeps generating (and billing) after the pane is gone, and shows
up as a "still streaming" resume target for a chat nobody reopens.
"""
import pytest

from routes import chat_routes
from src import agent_runs
from tests.routes.chat_routes.agent_turn import DONE, agent_turn, frame  # noqa: F401  (fixture)


@pytest.fixture
def plain_turn(agent_turn, monkeypatch):
    """A plain-chat model stream that records whether its chat has a detached run."""
    seen = []

    async def model_stream(candidates, messages, **kwargs):
        seen.append(agent_runs.is_active(agent_turn.session_id))
        yield frame({"delta": "hi"})
        yield DONE

    monkeypatch.setattr(chat_routes, "stream_llm_with_fallback", model_stream)
    return seen


def _post(agent_turn, **extra):
    return agent_turn.client.post("/api/chat_stream", data={
        "message": "hello", "session": agent_turn.session_id, "mode": "chat", **extra,
    })


def test_a_compare_pane_is_not_a_detached_run(agent_turn, plain_turn):
    response = _post(agent_turn, compare_mode="true")

    assert response.status_code == 200, response.text
    assert plain_turn == [False]
    assert "X-Odysseus-Run-Id" not in response.headers


def test_a_normal_chat_turn_is_a_detached_run(agent_turn, plain_turn):
    response = _post(agent_turn)

    assert response.status_code == 200, response.text
    assert plain_turn == [True]
    assert response.headers.get("X-Odysseus-Run-Id")
