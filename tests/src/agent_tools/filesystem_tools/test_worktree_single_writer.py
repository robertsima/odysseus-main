"""One writer per worktree, and one hand-back per worker run (2026-10-01).

An orchestrator chat kept apply_patch-ing the worktree its Lead Engineer
workers were running in, and one worker's hand-back reached the parent twice.
"""
import asyncio
import os

import pytest

from src import agent_activity as act
from src import agent_control, agent_runs, constants, worktree_writers
from src.agent_tools.filesystem_tools import ApplyPatchTool, EditFileTool, ReadFileTool, WriteFileTool
from src.tool_execution import _active_workspace


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "appdata"))
    act._reset_for_tests()
    monkeypatch.setattr(agent_runs, "is_busy", lambda sid: False)
    worktree_writers._LIVE.clear()
    yield
    worktree_writers._LIVE.clear()
    act._reset_for_tests()


def _bind(tmp_path):
    ws = tmp_path / "wt"
    ws.mkdir()
    (ws / "a.txt").write_text("one\n", encoding="utf-8")
    token = _active_workspace.set(str(ws))
    return ws, token


def _worker(ws, sid="w-1", parent="chat", run="run-1", owner="alice", label="Lead Engineer"):
    worktree_writers.register(run, session_id=sid, parent_session=parent, owner=owner,
                              workspace=str(ws), label=label)


async def test_the_launching_chat_cannot_write_into_a_live_workers_worktree(tmp_path):
    ws, token = _bind(tmp_path)
    try:
        _worker(ws)
        ctx = {"session_id": "chat", "owner": "alice"}
        patch = "*** Begin Patch\n*** Update File: a.txt\n@@\n-one\n+two\n*** End Patch"
        for res in (
            await WriteFileTool().execute(f"{ws / 'a.txt'}\nnew", ctx),
            await EditFileTool().execute(f'{{"path": "{(ws / "a.txt").as_posix()}", "old_string": "one", "new_string": "x"}}', ctx),
            await ApplyPatchTool().execute(patch, ctx),
        ):
            assert res["exit_code"] == 1
            assert "Lead Engineer (worker w-1)" in res["error"]
            assert "send_to_session" in res["error"] and "wait_seconds" in res["error"]
        assert (ws / "a.txt").read_text(encoding="utf-8") == "one\n"
        # A patch is judged on every file it touches, and reads stay open.
        read = await ReadFileTool().execute(str(ws / "a.txt"), ctx)
        assert read["exit_code"] == 0
    finally:
        _active_workspace.reset(token)


async def test_the_worker_its_sub_workers_and_co_tenants_may_write(tmp_path):
    ws, token = _bind(tmp_path)
    try:
        _worker(ws)
        # sub-worker of w-1, with its own run
        worktree_writers.register("run-2", session_id="w-2", parent_session="w-1", owner="alice",
                                  workspace=str(ws), label="Implementor")
        for sid in ("w-1", "w-2"):
            res = await WriteFileTool().execute(f"{ws / 'a.txt'}\nx", {"session_id": sid, "owner": "alice"})
            assert res["exit_code"] == 0, res
        # a sibling sharing the same workspace is a co-tenant, not blocked
        worktree_writers.register("run-3", session_id="w-3", parent_session="chat", owner="alice",
                                  workspace=str(ws), label="Other")
        res = await WriteFileTool().execute(f"{ws / 'b.txt'}\nx", {"session_id": "w-3", "owner": "alice"})
        assert res["exit_code"] == 0, res
        # other owner's chat is not our business; outside the worktree is free
        assert worktree_writers.conflict("chat", "bob", [str(ws / "a.txt")]) is None
        assert worktree_writers.conflict("chat", "alice", [str(tmp_path / "elsewhere.txt")]) is None
    finally:
        _active_workspace.reset(token)


async def test_released_when_the_run_ends_and_fail_open(tmp_path, monkeypatch):
    ws, token = _bind(tmp_path)
    try:
        _worker(ws)
        assert worktree_writers.conflict("chat", "alice", [str(ws / "a.txt")])
        worktree_writers.unregister("run-1")
        assert worktree_writers.conflict("chat", "alice", [str(ws / "a.txt")]) is None
        _worker(ws)
        monkeypatch.setattr(worktree_writers, "_norm", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        assert worktree_writers.conflict("chat", "alice", [str(ws / "a.txt")]) is None
    finally:
        _active_workspace.reset(token)


# ── one hand-back per run ────────────────────────────────────────────────

class _Msg:
    def __init__(self, role, content, metadata=None):
        self.role, self.content, self.metadata = role, content, metadata or {}


class _Chat:
    def __init__(self, sid):
        self.id, self.name, self.model = sid, sid, "m"
        self.endpoint_url, self.context_length, self.headers = "http://llm.test/v1", 200_000, {}
        self.history = []

    def add_message(self, m):
        self.history.append(m)

    def get_context_messages(self):
        return [{"role": m.role, "content": m.content} for m in self.history]


class _Manager:
    def __init__(self, *chats):
        self.chats = {c.id: c for c in chats}

    def get_session(self, sid):
        return self.chats.get(sid)

    def save_sessions(self):
        pass


class _W:
    id, name = "ddc821f4", "Lead Engineer"


async def test_the_same_hand_back_through_both_paths_lands_once(monkeypatch):
    from src import headless_agent

    async def fake_headless(sess, messages, **kw):
        return "ok", []

    monkeypatch.setattr(headless_agent, "run_headless", fake_headless)
    # The parent is mid-turn (blocked in a status poll), as in the incident.
    monkeypatch.setattr(agent_runs, "is_busy", lambda sid: True)
    parent = _Chat("parent")
    parent.add_message(_Msg("user", "make actual app finished"))
    manager = _Manager(parent)
    worker = _W()
    # The worker's own end-of-run hand-off and the hand-up from the lead's
    # continuation, racing on one result.
    await asyncio.gather(
        agent_control._hand_off(manager, "parent", worker, "task", "result", "completed", "alice"),
        agent_control._hand_off(manager, "parent", worker, "task", "result", "completed", "alice"),
    )
    assert [m.metadata.get("source") for m in parent.history].count("worker") == 1
    # A genuinely different result from the same worker still lands.
    await agent_control._hand_off(manager, "parent", worker, "task", "second result", "completed", "alice")
    assert [m.metadata.get("source") for m in parent.history].count("worker") == 2
    # So does an identical one once the chat has replied in between.
    parent.add_message(_Msg("assistant", "noted"))
    await agent_control._hand_off(manager, "parent", worker, "task", "second result", "completed", "alice")
    assert [m.metadata.get("source") for m in parent.history].count("worker") == 3
    for t in list(agent_control._PENDING_HANDOFFS):
        t.cancel()
