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


def test_a_parent_chat_finds_the_runs_of_the_workers_it_started(data_dir):
    """The chat that launched a worker must be able to list that worker's run.

    The run is filed under the WORKER's chat, but the agent strip above the
    composer draws its rows from the parent's own feed and then reconciles them
    against this list. A run the parent could not find was marked interrupted,
    so sub-agents flashed up and vanished from the chat that started them.
    """
    act.run_started("worker-chat", "session", "Worker · audit", run_id="session-1", owner="alice",
                    data={"parent_session": "parent-chat", "target_session": "worker-chat"})
    act.run_started("other-chat", "session", "Unrelated worker", run_id="session-2", owner="alice",
                    data={"parent_session": "someone-else", "target_session": "other-chat"})

    from_parent = act.list_runs(session_id="parent-chat", active_only=True)
    assert [r["run_id"] for r in from_parent] == ["session-1"]
    assert from_parent[0]["session_id"] == "worker-chat"

    # The worker's own chat still finds it, and an unrelated chat does not.
    assert [r["run_id"] for r in act.list_runs(session_id="worker-chat")] == ["session-1"]
    assert act.list_runs(session_id="parent-chat", active_only=True) != act.list_runs(session_id="other-chat")


def test_a_status_event_from_the_parent_side_closes_the_run(data_dir):
    act.run_started("worker-chat", "session", "Worker · audit", run_id="session-3", owner="alice",
                    data={"parent_session": "parent-chat"})
    assert act.get_run("session-3")["status"] == "running"

    act.publish("parent-chat", "status", "Worker finished", source="session", run_id="session-3",
                data={"status": "incomplete", "rounds_exhausted": True, "max_rounds": 6})

    run = act.get_run("session-3")
    assert run["status"] == "incomplete"
    # The run still belongs to the worker's chat; the parent only closed it.
    assert run["session_id"] == "worker-chat"
    assert not act.list_runs(session_id="parent-chat", active_only=True)
    # A status event closes a run exactly as run_finished does, so it has to
    # say WHEN. Both surfaces that show a finished agent -- the strip above the
    # composer (which keeps a row for a few seconds after it ends) and the
    # Agents dashboard (which windows recent rows) -- select on `finished_at`,
    # so a terminal run without one is a row that vanishes the instant the work
    # ends instead of being seen as incomplete.
    assert run["finished_at"] >= run["started_at"]


def test_a_run_rebuilt_from_a_status_event_still_belongs_to_its_parent_chat(data_dir):
    """The chat that started a worker must not lose it when the record is gone.

    A record can be missing when its ``run_started`` was evicted by the
    registry cap or was written by an earlier process. ``_update_run`` rebuilds
    one from whatever event arrives next -- and a rebuilt record with an empty
    summary has no ``parent_session``, which is the only thing that lets the
    launching chat list the run at all.
    """
    act.run_started("worker-chat", "session", "Worker · audit", run_id="session-9", owner="alice",
                    data={"parent_session": "parent-chat", "target_session": "worker-chat"})
    act._runs.pop("session-9")          # the record is gone; the worker is not

    act.publish("parent-chat", "status", "Worker alpha failed", source="session", run_id="session-9",
                owner="alice", level="error",
                data={"status": "failed", "parent_session": "parent-chat",
                      "target_session": "worker-chat", "error": "provider rejected the credential"})

    rebuilt = act.get_run("session-9")
    assert rebuilt["status"] == "failed" and rebuilt["finished_at"]
    assert rebuilt["summary"]["parent_session"] == "parent-chat"
    assert [r["run_id"] for r in act.list_runs(session_id="parent-chat")] == ["session-9"]


def test_the_registry_cap_never_evicts_a_run_that_is_still_working(data_dir, monkeypatch):
    """Eviction frees finished runs, oldest first -- never live ones.

    The registry is the only server-side truth the agent strip has. Dropping a
    live run to make room makes a working agent disappear from the chat that
    started it, releases its slot in ``agent_control.live_children`` and makes
    ``has_active_run`` report an in-flight chat as idle.
    """
    monkeypatch.setattr(act, "MAX_RUNS", 3)
    live = act.run_started("worker-chat", "session", "Worker · long audit", owner="alice",
                           data={"parent_session": "parent-chat"})
    oldest_finished = act.run_started("chat", "bg_job", "job 0", owner="alice")
    act.run_finished("chat", "bg_job", oldest_finished, "job 0 done", owner="alice")
    for i in range(1, 5):
        rid = act.run_started("chat", "bg_job", f"job {i}", owner="alice")
        act.run_finished("chat", "bg_job", rid, f"job {i} done", owner="alice")

    assert act.get_run(live)["status"] == "running"
    assert act.has_active_run("worker-chat")
    assert [r["run_id"] for r in act.list_runs(session_id="parent-chat")] == [live]
    assert act.get_run(oldest_finished) is None, "finished runs are what makes room"


def test_the_global_feed_has_history_not_just_live_events(data_dir):
    """"All sessions" in the Workbench showed nothing until something new happened.

    `publish` appends to the originating session only, so the `*` key was never
    a stored session and `history("*")` returned an empty list. `subscribe`
    deliberately replays nothing for the global feed, so history is the only
    thing that could have filled that scope.
    """
    act.publish("chat-a", "message", "first", source="odysseus")
    act.publish("chat-b", "message", "second", source="session")
    act.publish("chat-a", "message", "third", source="claude_code")

    merged = act.history(act.GLOBAL_FEED, limit=50)

    assert [ev["title"] for ev in merged] == ["first", "second", "third"]
    assert {ev["session_id"] for ev in merged} == {"chat-a", "chat-b"}
    # and a single session's history is unchanged
    assert [ev["title"] for ev in act.history("chat-a")] == ["first", "third"]


def test_global_history_is_bounded(data_dir):
    for i in range(60):
        act.publish(f"chat-{i % 5}", "note", f"event {i}", source="system")
    assert len(act.history(act.GLOBAL_FEED, limit=10)) == 10


def test_note_progress_merges_into_the_run_without_publishing(data_dir):
    rid = act.run_started("s-prog", "session", "Worker: long task")
    before = act.last_seq("s-prog")
    act.note_progress(rid, round=3, input_tokens=1000)
    act.note_progress(rid, round=4, current_tool="read_file")
    # Counters change every round; they must not spend the 500-event ring.
    assert act.last_seq("s-prog") == before
    rec = act.get_run(rid)
    assert rec["progress"] == {"round": 4, "input_tokens": 1000, "current_tool": "read_file"}
    listed = act.list_runs(session_id="s-prog")[0]
    assert listed["progress"]["round"] == 4
    # A copy: mutating what a caller got never reaches the registry.
    listed["progress"]["round"] = 99
    assert act.get_run(rid)["progress"]["round"] == 4
    # Unknown runs and a missing id are ignored, never raised.
    act.note_progress("nope", round=1)
    act.note_progress(None, round=1)


def test_note_progress_is_persisted_at_a_bounded_rate(data_dir, monkeypatch):
    rid = act.run_started("s-prog2", "session", "Worker")
    saves = []
    real_save = act._save_runs
    monkeypatch.setattr(act, "_save_runs", lambda: (saves.append(1), real_save()))
    clock = [1000.0]
    monkeypatch.setattr(act.time, "time", lambda: clock[0])
    for i in range(10):
        act.note_progress(rid, round=i)
    assert len(saves) == 1
    clock[0] += act.PROGRESS_SAVE_S
    act.note_progress(rid, round=11)
    assert len(saves) == 2
    act._reset_for_tests()  # a restart reads back the last saved counters
    assert act.get_run(rid)["progress"]["round"] == 11
