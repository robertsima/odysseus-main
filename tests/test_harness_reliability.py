"""Poll backoff, result-aware loop-breaker, tool budget, MCP index refresh,
MCP connection ownership, worker run status, and worktree next steps."""

import asyncio
import json
import threading

import anyio
import pytest
from contextlib import asynccontextmanager

import src.agent_loop as al
from src.agent_tools import claude_code_tools as cct


# ── poll backoff ──────────────────────────────────────────────────────────

def test_repeat_polls_wait_longer_each_time(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(cct.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cct, "_last_polls", {})
    waits = []
    for _ in range(6):
        waits.append(cct._poll_wait("task-1", 0))
        clock[0] += 5
    assert waits == [0, 30, 60, 120, 240, 240]
    # An explicit longer wait is honoured.
    assert cct._poll_wait("task-1", 500) == 500


def test_a_poll_after_a_quiet_spell_is_immediate_again(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(cct.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cct, "_last_polls", {})
    assert cct._poll_wait("t", 0) == 0
    assert cct._poll_wait("t", 0) == 30
    clock[0] += cct._POLL_REPEAT_WINDOW_S + 1
    assert cct._poll_wait("t", 0) == 0


def test_running_report_carries_a_progress_key_without_the_clock():
    rec = {"task_id": "t", "status": "running", "started_at": "2026-09-18T10:00:00+00:00"}
    a = cct._running_report(rec)
    assert "progress_key" in a
    assert json.loads(a["progress_key"])[0] == "running"


# ── result-aware loop-breaker ─────────────────────────────────────────────

def test_progress_digest_ignores_clocks_and_counters():
    a = {"status": "running", "elapsed_seconds": 31, "output": "step 1 of 4"}
    b = {"status": "running", "elapsed_seconds": 95, "output": "step 1 of 4"}
    assert al._result_progress_digest(a) == al._result_progress_digest(b)
    c = {"status": "completed", "elapsed_seconds": 95}
    assert al._result_progress_digest(a) != al._result_progress_digest(c)


def test_progress_digest_prefers_the_tools_own_key():
    a = {"progress_key": '["running", ["tool_start edit"]]', "note": "x"}
    b = {"progress_key": '["running", ["tool_start edit"]]', "note": "different text"}
    c = {"progress_key": '["running", ["tool_result edit done"]]', "note": "x"}
    assert al._result_progress_digest(a) == al._result_progress_digest(b)
    assert al._result_progress_digest(a) != al._result_progress_digest(c)


# ── tool budget ───────────────────────────────────────────────────────────

def _seeded(*domains):
    tools = set()
    for d in domains:
        tools |= al._DOMAIN_TOOL_MAP[d] | al._DOMAIN_EXTRA_TOOLS.get(d, set())
    return tools


def test_budget_drops_the_domains_retrieval_did_not_support():
    retrieved = {"ask_user", "read_app_logs", "delegate_to_claude_code", "grep", "read_file"}
    tools = _seeded("email", "cookbook", "files") | retrieved
    dropped = al._apply_tool_budget(tools, protected=retrieved, retrieved=retrieved,
                                    domains={"email", "cookbook", "files"}, budget=25)
    assert set(dropped) == {"email", "cookbook"}
    assert al._DOMAIN_TOOL_MAP["files"] <= tools
    assert retrieved <= tools
    assert len(tools) <= 25


def test_budget_never_drops_protected_tools():
    retrieved = {"send_email"}
    tools = _seeded("email") | retrieved
    al._apply_tool_budget(tools, protected={"send_email", "list_emails"}, retrieved=retrieved,
                          domains={"email"}, budget=1)
    assert {"send_email", "list_emails"} <= tools


def test_budget_off_or_not_reached_changes_nothing():
    tools = _seeded("email", "cookbook")
    before = set(tools)
    assert al._apply_tool_budget(tools, protected=set(), retrieved=set(),
                                 domains={"email", "cookbook"}, budget=0) == {}
    assert al._apply_tool_budget(tools, protected=set(), retrieved=set(),
                                 domains={"email", "cookbook"}, budget=500) == {}
    assert tools == before


# ── MCP index refresh ─────────────────────────────────────────────────────

class _Collection:
    def __init__(self):
        self.rows = {}
        self.upserts = []

    def get(self, where=None):
        return {"ids": [k for k, v in self.rows.items() if v.get("tool_type") == "mcp"]}

    def delete(self, ids):
        for i in ids:
            self.rows.pop(i, None)

    def upsert(self, ids, documents, embeddings, metadatas):
        self.upserts.append(list(ids))
        for i, m in zip(ids, metadatas):
            self.rows[i] = m


class _Lane:
    name = "fake"

    def __init__(self):
        self.collection = _Collection()

    def encode(self, docs):
        return [[0.0] for _ in docs]


class _Mgr:
    def __init__(self, text, gen=1):
        self.text = text
        self._generation = gen

    def get_tool_descriptions_for_prompt(self, disabled):
        return self.text


def _index():
    from src.tool_index import ToolIndex
    idx = object.__new__(ToolIndex)
    idx._lanes = [_Lane()]
    idx._mcp_generation = -1
    idx._mcp_docs = None
    idx._mcp_indexed_at = 0.0
    idx._mcp_index_lock = threading.Lock()
    return idx


def test_mcp_refresh_only_re_embeds_changed_tools():
    idx = _index()
    coll = idx._lanes[0].collection
    coll.rows["mcp_stale_from_last_process"] = {"tool_type": "mcp"}
    idx.index_mcp_tools(_Mgr("**A:**\n- mcp__a__one: first\n- mcp__a__two: second", gen=1))
    assert "mcp_stale_from_last_process" not in coll.rows
    assert sorted(coll.upserts[-1]) == ["mcp_mcp__a__one", "mcp_mcp__a__two"]

    idx.index_mcp_tools(_Mgr("**A:**\n- mcp__a__one: first\n- mcp__a__three: new", gen=2))
    assert coll.upserts[-1] == ["mcp_mcp__a__three"]
    assert "mcp_mcp__a__two" not in coll.rows
    assert idx.mcp_index_state(_Mgr("", gen=2))["current"] is True


def test_a_second_refresh_does_not_run_while_one_is_in_progress():
    idx = _index()
    idx._mcp_index_lock.acquire()
    try:
        idx.index_mcp_tools(_Mgr("**A:**\n- mcp__a__one: first", gen=5))
    finally:
        idx._mcp_index_lock.release()
    assert idx._lanes[0].collection.upserts == []
    assert idx._mcp_generation == -1


# ── MCP connection ownership ─────────────────────────────────────────────

@asynccontextmanager
async def _task_group_client():
    async with anyio.create_task_group() as tg:
        tg.start_soon(anyio.sleep_forever)
        yield "session"
        tg.cancel_scope.cancel()


async def test_owned_stack_closes_from_another_task():
    from src.mcp_manager import _OwnedStack

    async def setup(stack):
        return await stack.enter_async_context(_task_group_client())

    owned = _OwnedStack("srv")
    assert await asyncio.create_task(owned.open(setup)) == "session"
    await asyncio.create_task(owned.aclose())
    assert owned._task.done() and owned._task.exception() is None


async def test_owned_stack_surfaces_the_setup_error_itself():
    from src.mcp_manager import _OwnedStack

    async def setup(stack):
        await stack.enter_async_context(_task_group_client())
        raise RuntimeError("handshake failed")

    with pytest.raises(RuntimeError, match="handshake failed"):
        await _OwnedStack("srv").open(setup)


async def test_owned_stack_is_torn_down_when_the_connect_times_out():
    from src.mcp_manager import _OwnedStack

    async def setup(stack):
        await stack.enter_async_context(_task_group_client())
        await asyncio.sleep(10)

    owned = _OwnedStack("srv")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(owned.open(setup), 0.1)
    await asyncio.sleep(0.05)
    assert owned._task.done()


# ── worker run status ─────────────────────────────────────────────────────

async def test_a_sub_agent_stopped_for_approval_is_not_reported_completed(tmp_path, monkeypatch):
    from src import agent_activity as act
    from src import constants
    from src.agent_tools import session_tools as st

    class _Session:
        id, name, endpoint_url, model, headers, owner, context_length = (
            "child-9", "Worker", "http://llm.test/v1", "m", {}, "alice", 0)

        def __init__(self):
            self.history = []

        def get_context_messages(self):
            return []

        def add_message(self, msg):
            self.history.append(msg)

    sess = _Session()

    class _Mgr:
        def get_session(self, sid):
            return sess if sid == sess.id else None

        def save_sessions(self):
            pass

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    act._reset_for_tests()
    monkeypatch.setattr(st, "get_session_manager", lambda: _Mgr())

    async def fake_loop(url, model, messages, **kwargs):
        card = {"kind": "tool_approval", "approval_id": "ap-1", "description": "risky"}
        yield f'data: {json.dumps({"type": "tool_output", "tool": "bash", "command": "rm -rf x", "output": "Waiting", "exit_code": None, "ask_user": card})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(al, "stream_agent_loop", fake_loop)
    out = await st.send_to_session(json.dumps({"session_id": "child-9", "message": "clean up", "mode": "agent"}),
                                   session_id="parent-9", owner="alice")
    assert out["status"] == "waiting_approval"
    assert out["awaiting_approval"]["tool"] == "bash"
    assert "not finished" in out["response"]
    run = act.list_runs(session_id="parent-9")[0]
    assert run["status"] == "waiting_approval"
    # The card is saved in the worker's chat so it can be decided there.
    saved = sess.history[-1].metadata["tool_events"][0]
    assert saved["ask_user"]["approval_id"] == "ap-1"
    act._reset_for_tests()


# ── worktree next steps ───────────────────────────────────────────────────

async def test_worktree_actions_without_a_name_say_what_to_do():
    from src.agent_tools.worktree_tools import AgentWorktreeTool

    out = await AgentWorktreeTool().execute(json.dumps({"action": "request_publish"}), {})
    assert out["exit_code"] == 1
    assert out["code"] == "MISSING_BRANCH"
    assert out["next_action"] == {"action": "status"}


async def test_worktree_not_started_points_at_start(monkeypatch):
    from src.agent_tools.worktree_tools import AgentWorktreeTool
    from src.agent_worktree import service

    async def not_started(branch, **kwargs):
        raise service.WorktreeError("worktree does not exist yet; start it first",
                                    code="WORKTREE_NOT_STARTED")

    monkeypatch.setattr(service, "request_publish", not_started)
    out = await AgentWorktreeTool().execute(json.dumps({"action": "request_publish", "name": "cache-fix"}), {})
    assert out["code"] == "WORKTREE_NOT_STARTED"
    assert out["next_action"] == {"action": "start", "name": "cache-fix"}


# ── GitHub fetch retries network failures only ────────────────────────────

def _fake_fetch_env(monkeypatch, failures):
    from types import SimpleNamespace
    from src.agent_worktree import repository_remote as rr

    calls = []

    class _Client:
        def fetch(self, path, repo, determine_wants):
            calls.append(1)
            if failures:
                raise failures.pop(0)
            return SimpleNamespace(refs={b"refs/heads/dev": b"abc"})

    monkeypatch.setattr(rr, "_client", lambda url, token: (_Client(), "/owner/repo"))
    monkeypatch.setattr(rr.time, "sleep", lambda s: None)
    return rr, calls, SimpleNamespace(object_store={b"abc"})


def test_fetch_retries_a_timeout_then_succeeds(monkeypatch):
    rr, calls, repo = _fake_fetch_env(monkeypatch, [TimeoutError("Read timed out. (read timeout=60.0)")])
    assert rr._fetch_exact(repo, "https://github.com/o/r", b"refs/heads/dev", None) == b"abc"
    assert len(calls) == 2


def test_fetch_gives_up_after_bounded_attempts(monkeypatch):
    from src.agent_worktree.repository_sync import RepositorySyncError

    errors = [TimeoutError("Read timed out") for _ in range(5)]
    rr, calls, repo = _fake_fetch_env(monkeypatch, errors)
    with pytest.raises(RepositorySyncError) as info:
        rr._fetch_exact(repo, "https://github.com/o/r", b"refs/heads/dev", None)
    assert len(calls) == rr.FETCH_ATTEMPTS
    assert "Tried 3 times" in str(info.value)


def test_fetch_does_not_retry_a_missing_repository(monkeypatch):
    from src.agent_worktree.repository_sync import RepositorySyncError

    rr, calls, repo = _fake_fetch_env(monkeypatch, [RuntimeError("404 Not Found")])
    with pytest.raises(RepositorySyncError):
        rr._fetch_exact(repo, "https://github.com/o/r", b"refs/heads/dev", None)
    assert len(calls) == 1
