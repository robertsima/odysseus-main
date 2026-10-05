"""A message that replaces a running turn lands after that turn's partial reply.

Sending while a turn runs stops it. The new message used to be saved first and
the stopped turn's text afterwards, so the transcript read "did you get stuck?"
followed by the answer to the question before it (2026-09-29).
"""
import asyncio
from urllib.parse import urlencode

import pytest

from routes import chat_routes
from tests.routes.chat_routes.agent_turn import agent_turn, frame  # noqa: F401  (fixture)


async def _post_on_loop(api, username, path, form, on_body=None):
    """POST a form into the ASGI app on the running event loop.

    TestClient runs every request on its own loop and returns only when the
    response is complete, so two overlapping streams cannot share the state
    the route keeps per loop (agent_runs). This sends the request on the
    test's loop instead; ``on_body`` sees each chunk as it arrives.
    """
    from routes.auth_routes import SESSION_COOKIE

    token = api.auth.create_session_trusted(username)
    body = urlencode(form).encode()
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"content-length", str(len(body)).encode()),
            (b"cookie", f"{SESSION_COOKIE}={token}".encode()),
        ],
        "client": ("testclient", 50000), "server": ("testserver", 80),
    }
    sent = False
    never = asyncio.Event()
    chunks = []

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await never.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            chunk = message.get("body", b"").decode("utf-8", "replace")
            chunks.append(chunk)
            if on_body is not None:
                on_body(chunk)

    await api.app(scope, receive, send)
    return "".join(chunks)


@pytest.mark.asyncio
async def test_the_stopped_turn_is_saved_before_the_message_that_replaced_it(api, agent_turn, monkeypatch):
    started = asyncio.Event()
    turns = []

    async def loop(*args, **kwargs):
        turns.append(kwargs)
        if len(turns) == 1:
            yield frame({"delta": "working on it"})
            await asyncio.sleep(3600)
        else:
            yield frame({"delta": "done"})
            yield "data: [DONE]\n\n"

    monkeypatch.setattr(chat_routes, "stream_agent_loop", loop)

    def watch(chunk):
        if "working on it" in chunk:
            started.set()

    form = {"session": agent_turn.session_id, "mode": "agent"}
    first = asyncio.create_task(_post_on_loop(
        api, "admin", "/api/chat_stream", {**form, "message": "build the report"}, on_body=watch))
    await asyncio.wait_for(started.wait(), 30)

    await asyncio.wait_for(
        _post_on_loop(api, "admin", "/api/chat_stream", {**form, "message": "did you get stuck?"}), 30)
    await asyncio.wait_for(first, 30)

    history = agent_turn.client.get(f"/api/history/{agent_turn.session_id}").json()["history"]
    assert [(m["role"], m["content"]) for m in history] == [
        ("user", "build the report"),
        ("assistant", "working on it"),
        ("user", "did you get stuck?"),
        ("assistant", "done"),
    ]
