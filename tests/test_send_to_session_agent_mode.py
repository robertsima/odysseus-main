"""send_to_session as a real sub-agent, and the hand-off on the activity feed."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from src import agent_activity as act
from src import constants
from src.agent_tools import session_tools as st


class _Session:
    def __init__(self, sid, owner="alice"):
        self.id = sid
        self.name = "Coding chat"
        self.endpoint_url = "http://llm.test/v1"
        self.model = "worker-model"
        self.headers = {}
        self.owner = owner
        self.context_length = 0
        self.history = []

    def get_context_messages(self):
        return [{"role": "user", "content": "earlier"}]

    def add_message(self, msg):
        self.history.append(msg)


class _Manager:
    def __init__(self, sess):
        self._sess = sess
        self.saved = 0

    def get_session(self, sid):
        return self._sess if sid == self._sess.id else None

    def save_sessions(self):
        self.saved += 1


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    act._reset_for_tests()
    sess = _Session("child-1")
    mgr = _Manager(sess)
    monkeypatch.setattr(st, "get_session_manager", lambda: mgr)
    yield sess, mgr
    act._reset_for_tests()


def _sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


async def test_agent_mode_runs_the_loop_with_tools_and_reports_steps(env, monkeypatch):
    sess, mgr = env
    seen = {}

    async def fake_loop(url, model, messages, **kwargs):
        seen["kwargs"] = kwargs
        seen["messages"] = messages
        yield _sse({"type": "tool_start", "tool": "grep", "command": "pattern", "round": 1})
        yield _sse({"type": "tool_output", "tool": "grep", "command": "pattern", "output": "a.py:1: hit", "exit_code": 0})
        yield _sse({"delta": "Found ", "thinking": True})
        yield _sse({"delta": "Found it in a.py"})
        yield "data: [DONE]\n\n"

    import src.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)

    out = await st.send_to_session(json.dumps({"session_id": "child-1", "message": "find the bug", "mode": "agent"}),
                                   session_id="parent-1", owner="alice")

    assert out["response"] == "Found it in a.py" and out["mode"] == "agent" and out["tool_calls"] == 1
    assert seen["messages"][-1] == {"role": "user", "content": "find the bug"}
    blocked = seen["kwargs"]["disabled_tools"]
    assert {"send_to_session", "delegate_to_claude_code", "pipeline"} <= set(blocked), "no recursive fan-out"
    assert seen["kwargs"]["session_id"] == "child-1" and seen["kwargs"]["owner"] == "alice"

    # The child chat keeps the exchange, with the tool cards the UI knows how to draw.
    roles = [m.role for m in sess.history]
    assert roles == ["user", "assistant"]
    assert sess.history[1].metadata["tool_events"][0]["tool"] == "grep"
    assert sess.history[0].metadata["source"] == "agent" and mgr.saved == 1

    kinds = [e["kind"] for e in act.history("parent-1")]
    assert kinds == ["run_started", "message", "tool_start", "tool_result", "message", "run_finished"]
    run = act.list_runs(session_id="parent-1")[0]
    assert run["status"] == "completed" and run["source"] == "session"
    assert run["summary"]["target_session"] == "child-1" and run["summary"]["steps"] == 1


async def test_chat_mode_stays_a_single_reply(env, monkeypatch):
    sess, mgr = env

    async def fake_call(url, model, messages, headers=None, timeout=None):
        return "one reply"

    import src.llm_core as llm_core
    monkeypatch.setattr(llm_core, "llm_call_async", fake_call)

    out = await st.send_to_session("child-1\nhello there", session_id="parent-2", owner="alice")
    assert out == {"session_id": "child-1", "session_name": "Coding chat", "response": "one reply", "mode": "chat"}
    assert "tool_events" not in (sess.history[1].metadata or {})
    kinds = [e["kind"] for e in act.history("parent-2")]
    assert kinds == ["run_started", "message", "message", "run_finished"]


async def test_bad_mode_and_missing_args_are_rejected(env):
    out = await st.send_to_session(json.dumps({"session_id": "child-1", "message": "x", "mode": "yolo"}), owner="alice")
    assert "mode must be" in out["error"]
    out = await st.send_to_session("only-one-line", owner="alice")
    assert "error" in out


async def test_failure_closes_the_run_as_failed(env, monkeypatch):
    async def boom(url, model, messages, headers=None, timeout=None):
        raise RuntimeError("model down")

    import src.llm_core as llm_core
    monkeypatch.setattr(llm_core, "llm_call_async", boom)
    out = await st.send_to_session("child-1\nhi", session_id="parent-3", owner="alice")
    assert "model down" in out["error"]
    run = act.list_runs(session_id="parent-3")[0]
    assert run["status"] == "failed"


def test_native_args_with_mode_are_converted_to_json():
    from src.tool_schemas import function_call_to_tool_block
    block = function_call_to_tool_block("send_to_session", json.dumps({"session_id": "s", "message": "m", "mode": "agent"}))
    assert json.loads(block.content) == {"session_id": "s", "message": "m", "mode": "agent"}
    legacy = function_call_to_tool_block("send_to_session", json.dumps({"session_id": "s", "message": "m"}))
    assert legacy.content == "s\nm"


async def test_profile_starts_a_new_child_chat_with_its_model_tools_and_instructions(env, monkeypatch):
    parent, mgr = env
    created = {}

    def create_session(session_id, name, endpoint_url, model, rag, owner):
        child = _Session(session_id, owner=owner)
        child.name, child.endpoint_url, child.model = name, endpoint_url, model
        child.get_context_messages = lambda: []
        created["child"] = child
        return child

    mgr.create_session = create_session
    real_get = mgr.get_session
    mgr.get_session = lambda sid: created["child"] if created.get("child") and sid == created["child"].id else real_get(sid)

    import src.agent_profiles as ap
    import core.database as database
    monkeypatch.setattr(ap, "load_profiles", lambda: ap.validate_profiles([
        {"name": "researcher", "description": "reads the web", "model": "cheap-model",
         "instructions": "Only research; never edit files.", "disabled_tools": ["bash", "write_file"], "max_rounds": 5},
    ]))
    monkeypatch.setattr(st, "_resolve_model", lambda spec, owner=None: ("http://cheap.test/v1", spec, {}))
    saved = {}
    monkeypatch.setattr(database, "update_session_settings", lambda sid, patch: saved.setdefault(sid, patch))
    seen = {}

    async def fake_loop(url, model, messages, **kwargs):
        seen.update(url=url, model=model, messages=messages, **kwargs)
        yield _sse({"delta": "Summary of findings"})
        yield "data: [DONE]\n\n"

    import src.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)

    out = await st.send_to_session(json.dumps({"session_id": "new", "message": "compare pricing", "profile": "researcher"}),
                                   session_id="child-1", owner="alice")
    child = created["child"]
    assert out["response"] == "Summary of findings" and out["session_id"] == child.id and out["mode"] == "agent"
    assert child.name.startswith("↳ researcher: compare pricing") and seen["model"] == "cheap-model"
    assert seen["messages"][0] == {"role": "system", "content": "Only research; never edit files."}
    assert {"bash", "write_file", "send_to_session"} <= set(seen["disabled_tools"]) and seen["max_rounds"] == 5
    assert saved[child.id]["parent_session"] == "child-1" and saved[child.id]["agent_profile"] == "researcher"
    run = act.list_runs(session_id="child-1")[0]
    assert run["summary"]["target_session"] == child.id


async def test_unknown_profile_lists_the_available_ones(env, monkeypatch):
    import src.agent_profiles as ap
    monkeypatch.setattr(ap, "load_profiles", lambda: ap.validate_profiles([{"name": "coder"}]))
    out = await st.send_to_session(json.dumps({"session_id": "new", "message": "x", "profile": "ghost"}), owner="alice")
    assert "No agent profile named 'ghost'" in out["error"] and "coder" in out["error"]
