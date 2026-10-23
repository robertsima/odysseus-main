"""SSE ids and reconnect catch-up for the activity feed.

The Workbench reconnects with the position it last saw. A session's position
is its own `seq`; the global feed has no single sequence, so its position is a
cursor holding one `seq` per session.
"""
import asyncio
import json

import pytest

from src import agent_activity as act
from src import constants


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    act._reset_for_tests()
    yield tmp_path
    act._reset_for_tests()


def _parse(frame):
    out = {}
    for line in frame.splitlines():
        if line.startswith("id: "):
            out["id"] = line[4:]
        elif line.startswith("data: "):
            out["data"] = json.loads(line[6:])
    return out


async def _take(gen, until):
    """Frames from ``gen`` up to and including the first one ``until`` accepts."""
    frames = []
    async for frame in gen:
        parsed = _parse(frame)
        frames.append(parsed)
        if until(parsed):
            break
    return frames


def _is_ready(parsed):
    return (parsed.get("data") or {}).get("type") == "ready"


async def test_session_frames_carry_their_seq_as_id(data_dir):
    for i in range(3):
        act.publish("s1", "message", f"m{i}")
    frames = await asyncio.wait_for(_take(act.subscribe("s1"), _is_ready), 2)
    events = [f for f in frames if "title" in f["data"]]
    assert [f["id"] for f in events] == ["1", "2", "3"]


async def test_session_reconnect_replays_only_what_was_missed(data_dir):
    """Reconnecting with the original `since` replayed the whole timeline, so
    every event on screen appeared twice."""
    for i in range(3):
        act.publish("s1", "message", f"m{i}")
    act.publish("s1", "message", "while away")
    frames = await asyncio.wait_for(_take(act.subscribe("s1", since_seq=3), _is_ready), 2)
    assert [f["data"]["title"] for f in frames if "title" in f["data"]] == ["while away"]
    assert not any(f["data"].get("type") == "gap" for f in frames)


async def test_global_feed_reconnect_receives_events_emitted_while_disconnected(data_dir):
    act.publish("sA", "message", "a1")
    act.publish("sB", "message", "b1")

    gen = act.subscribe(act.GLOBAL_FEED, heartbeat_s=0.05)
    first = await asyncio.wait_for(_take(gen, _is_ready), 2)
    cursor_text = first[-1]["data"]["cursor"]
    act.publish("sA", "message", "a2 live")
    live = await asyncio.wait_for(_take(gen, lambda p: "title" in p["data"]), 2)
    last_id = live[-1]["id"]
    await gen.aclose()

    act.publish("sA", "message", "a3 missed")
    act.publish("sC", "message", "c1 missed, new session")
    act.publish("sB", "message", "b2 missed")

    cursor = act.decode_cursor(last_id)
    assert cursor["s"]["sA"] == 2 and cursor["s"]["sB"] == 1, "the id holds every session's position"
    assert act.decode_cursor(cursor_text)["s"] == {"sA": 1, "sB": 1}

    gen = act.subscribe(act.GLOBAL_FEED, cursor=cursor, heartbeat_s=0.05)
    frames = await asyncio.wait_for(_take(gen, _is_ready), 2)
    await gen.aclose()
    titles = sorted(f["data"]["title"] for f in frames if "title" in f["data"])
    assert titles == ["a3 missed", "b2 missed", "c1 missed, new session"]


async def test_global_fresh_connect_replays_nothing_and_skips_old_sessions_events(data_dir):
    act.publish("sA", "message", "old")
    gen = act.subscribe(act.GLOBAL_FEED, heartbeat_s=0.05)
    frames = await asyncio.wait_for(_take(gen, _is_ready), 2)
    await gen.aclose()
    assert [f for f in frames if "title" in f["data"]] == []


async def test_a_gap_over_the_replay_cap_is_reported(data_dir, monkeypatch):
    """A client away for longer than the cap must be told it missed events,
    not handed a silently truncated list."""
    monkeypatch.setattr(act, "REPLAY_CAP", 5)
    for i in range(12):
        act.publish("s1", "message", f"m{i}")
    frames = await asyncio.wait_for(_take(act.subscribe("s1", since_seq=2), _is_ready), 2)
    gaps = [f["data"] for f in frames if f["data"].get("type") == "gap"]
    assert len(gaps) == 1 and gaps[0]["skipped"] == 5 and gaps[0]["session_id"] == "s1"
    assert [f["data"]["title"] for f in frames if "title" in f["data"]] == [f"m{i}" for i in range(7, 12)]


async def test_a_global_gap_over_the_cap_is_reported(data_dir, monkeypatch):
    monkeypatch.setattr(act, "REPLAY_CAP", 3)
    act.publish("sA", "message", "seed")
    cursor = act.global_cursor()
    for i in range(6):
        act.publish("sA" if i % 2 else "sB", "message", f"m{i}")
    gen = act.subscribe(act.GLOBAL_FEED, cursor=cursor, heartbeat_s=0.05)
    frames = await asyncio.wait_for(_take(gen, _is_ready), 2)
    await gen.aclose()
    gaps = [f["data"] for f in frames if f["data"].get("type") == "gap"]
    assert len(gaps) == 1 and gaps[0]["skipped"] == 3
    assert len([f for f in frames if "title" in f["data"]]) == 3


async def test_a_gap_is_reported_when_events_left_the_bounded_timeline(data_dir, monkeypatch):
    monkeypatch.setattr(act, "MAX_EVENTS_PER_SESSION", 10)
    act._reset_for_tests()
    for i in range(30):
        act.publish("s1", "message", f"m{i}")
    frames = await asyncio.wait_for(_take(act.subscribe("s1", since_seq=5), _is_ready), 2)
    gaps = [f["data"] for f in frames if f["data"].get("type") == "gap"]
    assert gaps and gaps[0]["skipped"] == 15  # seq 6..20 are gone


def test_unreadable_cursors_mean_no_cursor():
    assert act.decode_cursor("g1.%%%") is None
    assert act.decode_cursor("42") is None
    assert act.decode_cursor("") is None
    both = act.merge_cursors({"s": {"a": 1, "b": 5}, "t": 10.0}, {"s": {"a": 3}, "t": 12.0})
    assert both == {"s": {"a": 3, "b": 5}, "t": 10.0}
