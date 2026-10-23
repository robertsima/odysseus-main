"""The Phalanx stream shows a user only their own chats.

The shared activity feed's `ready` frame carries a resume cursor naming every
recently active session, whoever owns it. Phalanx forwards `ready` frames to
every viewer, so the cursor has to be dropped there or bob learns alice's
session ids and how busy they are.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from routes import agents_routes as ar
from src import agent_activity as act
from src import constants


class _Sessions:
    def __init__(self, by_user):
        self.by_user = by_user

    def get_sessions_for_user(self, user):
        return {sid: SimpleNamespace(archived=False) for sid in self.by_user.get(user, ())}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "data"))
    act._reset_for_tests()
    monkeypatch.setattr(ar, "effective_user", lambda request: "bob")
    yield
    act._reset_for_tests()


async def _frames_until_ready(resp):
    frames = []
    async for frame in resp.body_iterator:
        frames.append(frame)
        data = next((json.loads(ln[6:]) for ln in frame.splitlines() if ln.startswith("data: ")), {})
        if data.get("type") == "ready":
            break
    await resp.body_iterator.aclose()
    return frames


@pytest.mark.security
async def test_ready_frame_names_no_other_users_sessions(env):
    act.publish("alice-secret-chat", "message", "alice working", owner="alice")
    act.publish("bob-chat", "message", "bob working", owner="bob")
    router = ar.setup_agents_routes(_Sessions({"alice": ["alice-secret-chat"], "bob": ["bob-chat"]}))
    stream = next(r.endpoint for r in router.routes if r.path == "/api/agents/stream")
    resp = await stream(SimpleNamespace(state=SimpleNamespace(), headers={}), since=0)
    frames = await asyncio.wait_for(_frames_until_ready(resp), 2)
    assert frames, "the stream sent nothing"
    for frame in frames:
        for line in frame.splitlines():
            if line.startswith("data: "):
                ev = json.loads(line[6:])
                for value in ev.values():
                    if isinstance(value, str) and value.startswith("g1."):
                        assert "alice-secret-chat" not in json.dumps(act.decode_cursor(value))
        assert "alice-secret-chat" not in frame
