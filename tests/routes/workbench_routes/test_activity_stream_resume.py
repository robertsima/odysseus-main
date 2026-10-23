"""The activity stream resumes from the browser's Last-Event-ID."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from routes import workbench_routes as wr
from src import agent_activity as act
from src import constants


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "data"))
    act._reset_for_tests()
    monkeypatch.setattr(wr, "_admin", lambda request: "alice")
    yield
    act._reset_for_tests()


def _endpoints():
    return {(m, r.path): r.endpoint for r in wr.setup_workbench_routes().routes for m in r.methods}


def _req(last_event_id=None):
    headers = {"last-event-id": last_event_id} if last_event_id else {}
    return SimpleNamespace(state=SimpleNamespace(), headers=headers, app=SimpleNamespace(state=SimpleNamespace()))


async def _titles(resp):
    titles = []
    async for frame in resp.body_iterator:
        data = next(json.loads(ln[6:]) for ln in frame.splitlines() if ln.startswith("data: "))
        if data.get("type") == "ready":
            break
        if "title" in data:
            titles.append(data["title"])
    await resp.body_iterator.aclose()
    return titles


async def test_session_stream_resumes_after_last_event_id(env):
    """A browser's automatic reconnect resends the URL it first used (since=0)
    plus Last-Event-ID; the header must win or the whole timeline replays."""
    for i in range(3):
        act.publish("s1", "message", f"m{i}")
    stream = _endpoints()[("GET", "/api/workbench/activity/stream")]
    resp = await stream(_req("2"), session_id="s1", since=0, cursor="")
    assert await asyncio.wait_for(_titles(resp), 2) == ["m2"]


async def test_global_history_hands_out_a_cursor_the_stream_resumes_from(env):
    act.publish("sA", "message", "a1")
    eps = _endpoints()
    hist = await eps[("GET", "/api/workbench/activity")](_req(), session_id="*", since=0, limit=50)
    act.publish("sA", "message", "a2 after history")
    resp = await eps[("GET", "/api/workbench/activity/stream")](_req(), session_id="*", since=0, cursor=hist["cursor"])
    assert await asyncio.wait_for(_titles(resp), 2) == ["a2 after history"]
