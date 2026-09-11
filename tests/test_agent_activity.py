"""The unified agent activity feed: bounded, persisted, replayable, live."""
import asyncio
import json
import os

import pytest

from src import agent_activity as act
from src import constants


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    act._reset_for_tests()
    yield tmp_path
    act._reset_for_tests()


def test_publish_records_and_persists_bounded_events(data_dir):
    ev = act.publish("s1", "tool_start", "bash: ls", source="odysseus", detail="x" * 10000,
                     data={"command": "ls", "blob": "y" * 20000}, owner="alice")
    assert ev["seq"] == 1 and ev["session_id"] == "s1" and ev["source"] == "odysseus"
    assert len(ev["detail"]) <= act.MAX_DETAIL_CHARS
    assert len(json.dumps(ev["data"])) <= act.MAX_DATA_CHARS + 200
    path = os.path.join(str(data_dir), "agent_activity", "s1.jsonl")
    assert os.path.exists(path)
    stored = json.loads(open(path, encoding="utf-8").read().splitlines()[0])
    assert stored["title"] == "bash: ls" and stored["owner"] == "alice"


def test_unknown_kind_and_source_are_normalised_not_rejected(data_dir):
    ev = act.publish("s1", "explode", "weird", source="martian")
    assert ev["kind"] == "note" and ev["source"] == "system"


def test_history_survives_a_restart_and_is_ordered(data_dir):
    for i in range(5):
        act.publish("s2", "message", f"m{i}")
    act._reset_for_tests()  # simulate a process restart: memory gone, file stays
    rows = act.history("s2")
    assert [r["title"] for r in rows] == ["m0", "m1", "m2", "m3", "m4"]
    assert act.last_seq("s2") == 5
    nxt = act.publish("s2", "message", "m5")
    assert nxt["seq"] == 6, "sequence numbers continue after the persisted tail"
    assert [r["title"] for r in act.history("s2", since_seq=4)] == ["m4", "m5"]


def test_memory_ring_is_bounded(data_dir, monkeypatch):
    monkeypatch.setattr(act, "MAX_EVENTS_PER_SESSION", 20)
    act._reset_for_tests()
    for i in range(50):
        act.publish("s3", "message", f"m{i}")
    rows = act.history("s3", limit=100)
    assert len(rows) == 20 and rows[0]["title"] == "m30"


def test_runs_registry_tracks_start_and_finish(data_dir):
    rid = act.run_started("s4", "claude_code", "Claude Code: fix the parser", owner="alice",
                          data={"repository": "/repo", "model": "opus", "secret": "no"})
    runs = act.list_runs(owner="alice")
    assert runs and runs[0]["run_id"] == rid and runs[0]["status"] == "running"
    assert runs[0]["summary"] == {"repository": "/repo", "model": "opus"}
    act.publish("s4", "tool_start", "Edit src/parser.py", source="claude_code", run_id=rid)
    act.run_finished("s4", "claude_code", rid, "Claude Code finished", status="completed",
                     data={"changed_files": ["src/parser.py"], "commit": "abc"})
    rec = act.get_run(rid)
    assert rec["status"] == "completed" and rec["summary"]["changed_files"] == ["src/parser.py"]
    assert [e["kind"] for e in act.run_events(rid)] == ["run_started", "tool_start", "run_finished"]
    assert act.list_runs(active_only=True) == []


def test_running_runs_become_interrupted_after_restart(data_dir):
    rid = act.run_started("s5", "session", "sub-agent")
    act._reset_for_tests()
    assert act.get_run(rid)["status"] == "interrupted"


def test_runs_are_owner_scoped_in_listing(data_dir):
    act.run_started("s6", "bg_job", "job a", owner="alice")
    act.run_started("s6", "bg_job", "job b", owner="bob")
    act.run_started("s6", "system", "unowned")
    titles = sorted(r["title"] for r in act.list_runs(owner="alice"))
    assert titles == ["job a", "unowned"]


def test_publish_never_raises_even_when_storage_is_broken(data_dir, monkeypatch):
    monkeypatch.setattr(act, "activity_dir", lambda: "/proc/definitely/not/writable")
    ev = act.publish("s7", "message", "still fine")
    assert ev is not None and ev["title"] == "still fine"


async def test_subscribe_replays_then_streams_live(data_dir):
    act.publish("s8", "message", "before")
    frames = []

    async def consume():
        async for frame in act.subscribe("s8", heartbeat_s=0.05):
            frames.append(frame)
            if len(frames) >= 4:
                break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.02)
    act.publish("s8", "tool_start", "live one")
    act.publish("s9", "tool_start", "other session")  # must not leak in
    act.publish("s8", "tool_result", "live two")
    await asyncio.wait_for(task, timeout=2)
    payloads = [json.loads(f[6:]) for f in frames if f.startswith("data: ")]
    titles = [p.get("title") for p in payloads if "title" in p]
    assert titles == ["before", "live one", "live two"]
    assert any(p.get("type") == "ready" for p in payloads)
    assert all(p.get("session_id") != "s9" for p in payloads if "title" in p)


async def test_global_feed_filters_by_owner_and_emits_heartbeats(data_dir):
    frames = []

    async def consume():
        async for frame in act.subscribe(act.GLOBAL_FEED, owner="alice", heartbeat_s=0.05):
            frames.append(frame)
            if any(f.startswith(": heartbeat") for f in frames) and len([f for f in frames if "title" in f]) >= 2:
                break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.02)
    act.publish("sA", "message", "alice sees this", owner="alice")
    act.publish("sB", "message", "bob's private thing", owner="bob")
    act.publish("sC", "message", "unowned is visible")
    await asyncio.wait_for(task, timeout=2)
    titles = [json.loads(f[6:])["title"] for f in frames if f.startswith("data: ") and "title" in f]
    assert titles == ["alice sees this", "unowned is visible"]
