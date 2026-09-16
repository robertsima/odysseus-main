"""Agents dashboard API: owner-scoped overview, stop, steer, launch."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import src.agent_loop as agent_loop
from routes import agents_routes as ar
from src import agent_activity as act
from src import agent_control, agent_runs, constants, tool_approvals


class _Sess:
    def __init__(self, sid, name, owner="alice", model="m"):
        self.id, self.name, self.owner, self.model, self.archived = sid, name, owner, model, False
        self.endpoint_url, self.headers, self.context_length, self.history = "http://x", {}, 0, []

    def add_message(self, m):
        self.history.append(m)


class _Mgr:
    def __init__(self, sessions):
        self.sessions = {s.id: s for s in sessions}

    def get_sessions_for_user(self, user):
        return {k: v for k, v in self.sessions.items() if v.owner == user}

    def get_session(self, sid):
        return self.sessions.get(sid)

    def create_session(self, session_id, name, endpoint_url, model, rag, owner):
        s = _Sess(session_id, name, owner=owner, model=model)
        s.endpoint_url = endpoint_url
        self.sessions[session_id] = s
        return s

    def save_sessions(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    act._reset_for_tests(); tool_approvals._reset_for_tests()
    agent_runs._RUNS.clear(); agent_runs._EXTERNAL.clear(); agent_runs._FINISHED.clear()
    agent_control._STEER.clear()
    mgr = _Mgr([_Sess("a1", "Alice chat"), _Sess("b1", "Bob chat", owner="bob")])
    monkeypatch.setattr(ar, "effective_user", lambda request: "alice")
    import src.ai_interaction as ai
    monkeypatch.setattr(ai, "get_session_manager", lambda: mgr)
    import core.database as db
    monkeypatch.setattr(db, "get_session_settings", lambda sid: {})
    monkeypatch.setattr(db, "update_session_settings", lambda sid, patch: patch)
    # Fake endpoints must not perform a real context-window probe while a
    # worker/parent handoff is under its deterministic five-second test limit.
    import src.model_context as model_context
    monkeypatch.setattr(model_context, "get_context_length", lambda *args, **kwargs: 32_000)
    monkeypatch.setattr(model_context, "budget_context_for_model", lambda *args, **kwargs: 32_000)
    router = ar.setup_agents_routes(mgr)
    eps = {(m, r.path): r.endpoint for r in router.routes for m in r.methods}
    yield mgr, eps
    act._reset_for_tests(); tool_approvals._reset_for_tests(); agent_control._STEER.clear()


def _req(body=None):
    async def _json():
        return body
    return SimpleNamespace(json=_json)


def test_agent_routing_uses_the_human_request_not_injected_context():
    messages = [
        {"role": "user", "content": "Can I redistribute this fork under its license?"},
        {"role": "user", "content": "UNTRUSTED SOURCE DATA: manage settings, servers and agents",
         "metadata": {"trusted": False}},
    ]
    assert agent_loop._detect_admin_intent(messages) is False
    assert agent_loop._detect_admin_tools(messages) == set()
    assert agent_loop._explicit_delegation_requested(messages[0]["content"]) is False
    assert agent_loop._explicit_delegation_requested("Have Claude Code inspect this repository") is True


async def test_overview_is_owner_scoped_and_grouped(env):
    mgr, eps = env
    with agent_runs.track_external("a1", source="subagent", owner="alice"):
        act.run_started("a1", "session", "Sub-agent · worker: dig", owner="alice", data={"target_session": "x"})
        act.run_started("b1", "session", "Bob's worker", owner="bob")
        tool_approvals.request("a1", "bash", "git push", "pushes to a remote")
        out = await eps[("GET", "/api/agents/overview")](_req())
    assert [r["session_id"] for r in out["rows"]] == ["a1"]
    row = out["rows"][0]
    assert row["status"] == "waiting_approval" and row["pending_approvals"] == 1 and row["children_running"] == 1
    assert out["totals"]["waiting_approval"] == 1 and out["totals"]["workers_running"] == 1
    approvals = await eps[("GET", "/api/agents/approvals")](_req())
    assert [a["session_id"] for a in approvals["approvals"]] == ["a1"]


async def test_overview_includes_the_current_open_chat_without_agent_history(env):
    _mgr, eps = env
    out = await eps[("GET", "/api/agents/overview")](_req(), current_session="a1")
    assert [row["session_id"] for row in out["rows"]] == ["a1"]
    assert out["rows"][0]["is_current"] is True
    assert out["rows"][0]["config"]["delegation_policy"] == "explicit"


async def test_archive_is_owner_scoped_recoverable_and_refuses_active_descendants(env, monkeypatch):
    """Cleanup hides idle work only; it neither stops nor erases live work."""
    mgr, eps = env
    archive = eps[("POST", "/api/agents/sessions/{session_id}/archive")]
    from fastapi import HTTPException

    # Bob's session is never addressable through Alice's dashboard.
    with pytest.raises(HTTPException) as exc:
        await archive(_req(), session_id="b1")
    assert exc.value.status_code == 404

    with agent_runs.track_external("a1", source="bg_job", owner="alice"):
        with pytest.raises(HTTPException) as exc:
            await archive(_req(), session_id="a1")
        assert exc.value.status_code == 409
        assert mgr.sessions["a1"].archived is False

    out = await archive(_req(), session_id="a1")
    assert out["archived"] is True and "history" in out["message"]
    assert mgr.sessions["a1"].archived is True
    # The normal fleet no longer lists cleanup-hidden sessions.
    assert (await eps[("GET", "/api/agents/overview")](_req()))["rows"] == []


async def test_archive_refuses_an_active_child_and_cleanup_hides_only_completed_runs(env, monkeypatch):
    mgr, eps = env
    mgr.sessions["a2"] = _Sess("a2", "Alice worker")
    settings = {"a2": {"parent_session": "a1"}}
    import core.database as db
    monkeypatch.setattr(db, "get_session_settings", lambda sid: settings.get(sid, {}))
    monkeypatch.setattr(db, "update_session_settings", lambda sid, patch: settings.setdefault(sid, {}).update(patch) or settings[sid])
    archive = eps[("POST", "/api/agents/sessions/{session_id}/archive")]
    cleanup = eps[("POST", "/api/agents/sessions/{session_id}/cleanup-runs")]
    restore_runs = eps[("POST", "/api/agents/sessions/{session_id}/restore-runs")]
    from fastapi import HTTPException

    with agent_runs.track_external("a2", source="bg_job", owner="alice"):
        with pytest.raises(HTTPException) as exc:
            await archive(_req(), session_id="a1")
        assert exc.value.status_code == 409 and "a2" in exc.value.detail

    run_id = act.run_started("a1", "session", "finished child", owner="alice")
    act.run_finished("a1", "session", run_id, "finished child", owner="alice")
    result = await cleanup(_req({"run_ids": [run_id]}), session_id="a1")
    assert result["history_preserved"] is True and settings["a1"]["hidden_agent_runs"] == [run_id]
    # Cleanup is an affordance only: it does not delete the run or its events.
    assert act.get_run(run_id)["status"] == "completed" and act.run_events(run_id)
    restored = await restore_runs(_req({"run_ids": [run_id]}), session_id="a1")
    assert restored["restored_run_ids"] == [run_id] and settings["a1"]["hidden_agent_runs"] == []


async def test_archive_refuses_queued_cli_children_and_cleanup_is_owner_scoped(env, monkeypatch):
    mgr, eps = env
    mgr.sessions["a2"] = _Sess("a2", "Queued CLI child")
    settings = {"a2": {"parent_session": "a1"}}
    import core.database as db
    monkeypatch.setattr(db, "get_session_settings", lambda sid: settings.get(sid, {}))
    monkeypatch.setattr(agent_control, "live_children", lambda sid: 1 if sid == "a2" else 0)
    archive = eps[("POST", "/api/agents/sessions/{session_id}/archive")]
    cleanup = eps[("POST", "/api/agents/sessions/{session_id}/cleanup-runs")]
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await archive(_req(), session_id="a1")
    assert exc.value.status_code == 409 and "a2" in exc.value.detail

    # A foreign run cannot be hidden through Alice's parent chat endpoint.
    foreign = act.run_started("b1", "session", "Bob child", owner="bob")
    act.run_finished("b1", "session", foreign, "Bob child", owner="bob")
    with pytest.raises(HTTPException) as exc:
        await cleanup(_req({"run_ids": [foreign]}), session_id="a1")
    assert exc.value.status_code == 404


async def test_unarchive_checks_db_owner_before_restoring(env, monkeypatch):
    """Archived sessions can be absent from the live manager after restart."""
    _mgr, eps = env
    restore = eps[("POST", "/api/agents/sessions/{session_id}/unarchive")]
    import core.database as db
    from fastapi import HTTPException

    class _Field:
        def __eq__(self, other):
            return other

    class _DbSession:
        id = _Field()
        owner = _Field()

    row = SimpleNamespace(owner="alice", archived=True)
    class _Query:
        def filter(self, *_args): return self
        def first(self): return row
    class _Db:
        def query(self, *_args): return _Query()
        def commit(self): pass
        def close(self): pass
    monkeypatch.setattr(db, "Session", _DbSession)
    monkeypatch.setattr(db, "SessionLocal", lambda: _Db())

    out = await restore(_req(), session_id="a1")
    assert out["archived"] is False and row.archived is False
    row.owner, row.archived = "bob", True
    with pytest.raises(HTTPException) as exc:
        await restore(_req(), session_id="a1")
    assert exc.value.status_code == 404 and row.archived is True


async def test_archived_overview_uses_db_metadata_without_hydrating_transcripts(env, monkeypatch):
    """A restart must list archive cards without loading each full history."""
    mgr, eps = env
    mgr.sessions.pop("a1")
    import core.database as db

    class _Field:
        def __eq__(self, other): return other
    class _DbSession:
        id, owner, archived, name, model = _Field(), _Field(), _Field(), _Field(), _Field()
    row = SimpleNamespace(id="old-agent", name="Old agent", model="m", owner="alice", archived=True)
    class _Query:
        def filter(self, *_args): return self
        def all(self): return [row]
    class _Db:
        def query(self, *_args): return _Query()
        def close(self): pass
    monkeypatch.setattr(db, "Session", _DbSession)
    monkeypatch.setattr(db, "SessionLocal", lambda: _Db())
    monkeypatch.setattr(db, "get_session_settings", lambda _sid: {})
    mgr.get_session = lambda _sid: (_ for _ in ()).throw(AssertionError("must not hydrate archive history"))

    out = await eps[("GET", "/api/agents/overview")](_req(), archived=True)
    assert [(item["session_id"], item["name"], item["archived"]) for item in out["rows"]] == [("old-agent", "Old agent", True)]


async def test_steer_requires_a_running_chat_and_lands_in_the_next_round(env, monkeypatch):
    mgr, eps = env
    steer = eps[("POST", "/api/agents/sessions/{session_id}/steer")]
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await steer(_req({"text": "focus on tests"}), session_id="a1")
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException):
        await steer(_req({"text": "x"}), session_id="b1")

    # Detached work has a wrapper activity run; that stable id is the one a
    # headless loop drains, not the loop's private telemetry id.
    act.run_started("a1", "bg_job", "Background continuation", run_id="bg-wrapper", owner="alice")
    with agent_runs.track_external("a1", source="bg_job", owner="alice"):
        out = await steer(_req({"text": "focus on tests"}), session_id="a1")
    assert out["queued"] and agent_control.pending_steer("a1")[0]["text"] == "focus on tests"

    # The loop drains it at the start of a round as a user message.
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10, raising=False)
    seen = {}

    async def fake_stream(_candidates, messages, **kwargs):
        seen["messages"] = list(messages)
        yield 'data: {"delta": "ok"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream, raising=False)
    chunks = [c async for c in agent_loop.stream_agent_loop(
        "https://api.openai.com/v1", "gpt-4o", [{"role": "user", "content": "go"}],
        relevant_tools={"read_file"}, session_id="a1", _is_teacher_run=True,
        steer_run_id="bg-wrapper")]
    assert any('"steer_applied"' in c for c in chunks)
    assert agent_loop._extract_last_user_message(seen["messages"]).endswith("focus on tests")
    assert agent_control.pending_steer("a1") == []

    # Leaving the queue is no longer the end of the story: the message reached
    # `injected`, and says which round took it.
    row = agent_control.steer_history("a1")[0]
    assert (row["id"], row["state"], row["round"]) == (out["id"], "injected", 1)


async def test_steer_lifecycle_is_visible_to_the_control_room(env, monkeypatch):
    """The dashboard has to be able to show a steer that never landed — that
    is the failure being fixed — so state travels with the overview row and
    there is a per-chat log for after the turn is over."""
    mgr, eps = env
    steer = eps[("POST", "/api/agents/sessions/{session_id}/steer")]
    steer_log = eps[("GET", "/api/agents/sessions/{session_id}/steer")]
    from fastapi import HTTPException

    # Refused because nothing is running: the sender sees an error, and the
    # target chat's own record now mentions the attempt.
    with pytest.raises(HTTPException):
        await steer(_req({"text": "never lands"}), session_id="a1")
    refused = await steer_log(_req(), session_id="a1")
    assert refused["queued"] == 0
    assert refused["messages"][0]["state"] == "failed"
    assert "not queued" in refused["messages"][0]["reason"]

    act.run_started("a1", "bg_job", "Background continuation", run_id="bg-lifecycle", owner="alice")
    with agent_runs.track_external("a1", source="bg_job", owner="alice"):
        queued = await steer(_req({"text": "use the staging database"}), session_id="a1")
        ov = await eps[("GET", "/api/agents/overview")](_req())
        row = next(r for r in ov["rows"] if r["session_id"] == "a1")
        # The old count still means what it meant, and now has company.
        assert row["steer_queued"] == 1 and row["steer"]["queued"] == 1
        newest = row["steer"]["messages"][0]
        assert (newest["id"], newest["state"], newest["live"]) == (queued["id"], "queued", True)
        assert newest["text"] == "use the staging database"

    # Nothing drained it before the turn ended.
    agent_control.clear_steer("a1", run_id="bg-lifecycle")
    after = await steer_log(_req(), session_id="a1")
    assert after["queued"] == 0
    assert after["messages"][0]["state"] == "cancelled"
    assert "turn ended" in after["messages"][0]["reason"]

    with pytest.raises(HTTPException):  # still owner-scoped
        await steer_log(_req(), session_id="b1")


async def test_launch_starts_a_worker_chat_and_stop_ends_it(env, monkeypatch):
    mgr, eps = env
    started = asyncio.Event()

    async def slow_loop(url, model, messages, **kwargs):
        yield 'data: {"delta": "partial"}\n\n'
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(agent_loop, "stream_agent_loop", slow_loop)
    import src.agent_profiles as ap
    monkeypatch.setattr(ap, "load_profiles", lambda: ap.validate_profiles([{"name": "researcher", "model": "cheap"}]))
    import src.agent_tools.session_tools as st_mod
    monkeypatch.setattr(st_mod, "_resolve_model", lambda spec, owner=None: ("http://cheap", spec, {}))

    out = await eps[("POST", "/api/agents/launch")](_req({"task": "compare pricing", "profile": "researcher", "parent_session": "a1"}))
    child = mgr.sessions[out["session_id"]]
    assert child.owner == "alice" and child.model == "cheap" and child.name.startswith("↳ researcher")
    await started.wait()
    ov = await eps[("GET", "/api/agents/overview")](_req())
    statuses = {r["session_id"]: r["status"] for r in ov["rows"]}
    assert statuses[child.id] == "running"

    res = await eps[("POST", "/api/agents/runs/{run_id}/stop")](_req(), run_id=out["run_id"])
    assert res == {"stopped": True, "how": "headless"}
    await asyncio.wait_for(agent_control._WORKERS.get(out["run_id"]) or asyncio.sleep(0), 5)
    assert act.get_run(out["run_id"])["status"] == "cancelled"
    assert child.history[-1].content.startswith("partial")

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await eps[("POST", "/api/agents/launch")](_req({"task": "x", "parent_session": "b1"}))
    assert exc.value.status_code == 404


async def test_finished_worker_hands_its_result_to_the_parent_chat_which_continues(env, monkeypatch):
    mgr, eps = env
    parent = mgr.sessions["a1"]
    parent.get_context_messages = lambda: [{"role": m.role, "content": m.content} for m in parent.history]
    calls = []

    async def fake_loop(url, model, messages, **kwargs):
        calls.append([m["content"] for m in messages])
        yield 'data: {"delta": "' + ("worker result" if len(calls) == 1 else "parent continued") + '"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_loop)
    out = await eps[("POST", "/api/agents/launch")](_req({"task": "find prices", "parent_session": "a1"}))
    await asyncio.wait_for(agent_control._WORKERS[out["run_id"]], 5)
    roles = [(m.role, m.metadata.get("source")) for m in parent.history]
    assert roles == [("user", "worker"), ("assistant", "worker_followup")]
    assert "Result:\nworker result" in parent.history[0].content
    assert parent.history[1].content == "parent continued"
    assert calls[1][-1].startswith("[Worker")
    assert act.list_runs(session_id="a1")[0]["status"] == "completed"
