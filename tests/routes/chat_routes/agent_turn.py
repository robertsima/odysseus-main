"""An agent turn through POST /api/chat_stream with the agent loop scripted.

The route under test is real: auth, the request parsing, the tool-toggle and
privilege policy, the saving of the reply. Only ``stream_agent_loop`` (the
model and its tools) is replaced by a script of SSE frames, and it records
the arguments the route called it with; the endpoint's context-window lookup
answers without the network.

    def test_x(agent_turn):
        turn = agent_turn.send({"message": "hi"}, as_json=True)
        turn.loop_kwargs["disabled_tools"]
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

import src.database
from routes import chat_routes
from src import model_context


def frame(payload) -> str:
    return f"data: {json.dumps(payload)}\n\n"


DONE = "data: [DONE]\n\n"
REPLY = [frame({"delta": "Done."}), frame({"type": "metrics", "data": {}}), DONE]


@dataclass
class Turn:
    status: int
    body: str
    loop_kwargs: dict = field(default_factory=dict)


class AgentTurn:
    def __init__(self, client, session_id, monkeypatch):
        self.client = client
        self.session_id = session_id
        self.script = list(REPLY)
        self._calls = []

        async def scripted_loop(*args, **kwargs):
            self._calls.append(kwargs)
            for chunk in self.script:
                yield chunk

        monkeypatch.setattr(chat_routes, "stream_agent_loop", scripted_loop)

    def send(self, fields: dict, *, as_json: bool = False) -> Turn:
        """POST one turn as the composer's form fields, or as an API caller's JSON."""
        fields = {"session": self.session_id, **fields}
        self._calls.clear()
        if as_json:
            response = self.client.post("/api/chat_stream", json=fields)
        else:
            response = self.client.post("/api/chat_stream", data=fields)
        assert response.status_code == 200, response.text
        assert self._calls, f"the turn never reached the agent loop: {response.text[:500]}"
        return Turn(response.status_code, response.text, self._calls[-1])

    def saved_replies(self) -> list[dict]:
        """The assistant messages stored for this chat, oldest first."""
        response = self.client.get(f"/api/history/{self.session_id}")
        assert response.status_code == 200, response.text
        return [m for m in response.json()["history"] if m["role"] == "assistant"]


@pytest.fixture
def agent_turn(api, monkeypatch):
    """The admin's chat on a stored endpoint, with the agent loop scripted.

    The admin, because only an account allowed to use the shell can show
    whether a turn's shell toggle reached the tool policy.
    """
    db = src.database.SessionLocal()
    try:
        db.add(src.database.ModelEndpoint(
            id="admin-ep", name="admin endpoint", base_url="http://127.0.0.1:9/v1", is_enabled=True,
            owner="admin", cached_models=json.dumps(["test-model"]),
        ))
        db.commit()
    finally:
        db.close()
    # The route asks the endpoint for the model's context window; nothing
    # listens there, and on Windows each refused connect takes seconds.
    monkeypatch.setattr(model_context, "_query_context_length", lambda endpoint_url, model: (32768, True))
    admin = api.as_admin()
    created = admin.post("/api/session", data={
        "name": "tools", "endpoint_id": "admin-ep", "model": "test-model", "skip_validation": "true",
    })
    assert created.status_code == 200, created.text
    return AgentTurn(admin, created.json()["id"], monkeypatch)
