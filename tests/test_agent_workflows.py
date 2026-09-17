"""Real worker scheduling/persistence, mocked model only: fan-out, collect, synthesize."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from core.models import ChatMessage, Session
from src import agent_activity as activity, agent_control, agent_workflows as workflows
from src.agent_tools.workflow_tools import OrchestrateAgentsTool


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    from src import constants, agent_runs, headless_agent, agent_loadouts
    from src.agent_tools import session_tools
    import src.ai_interaction as ai
    import core.database as db

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    activity._reset_for_tests()
    monkeypatch.setattr(agent_control, "_WORKERS", {})
    monkeypatch.setattr(workflows, "_TASKS", {})
    monkeypatch.setattr(workflows, "_LIVE", {})
    parent = Session("parent", "Parent", "https://llm.test", "test-model", owner="alice")
    parent.add_message(ChatMessage("user", "Use specialist agents to research the market"))
    sessions, settings = {parent.id: parent}, {}
    manager = SimpleNamespace(get_session=sessions.get, save_sessions=lambda: None)
    monkeypatch.setattr(ai, "get_session_manager", lambda: manager)
    monkeypatch.setattr(db, "get_session_settings", lambda sid, **kwargs: copy.deepcopy(settings.get(sid, {})))

    def update(sid, patch):
        settings.setdefault(sid, {}).update(copy.deepcopy(patch))
        return copy.deepcopy(settings[sid])

    monkeypatch.setattr(db, "update_session_settings", update)
    def child(manager, parent_id, owner, message, profile):
        sid = f"child-{len(sessions)}"
        sess = Session(sid, profile["name"], "https://llm.test", profile["model"] or "test-model", owner=owner)
        sessions[sid] = sess
        return sess, None
    monkeypatch.setattr(session_tools, "_new_child_session", child)
    tools = set(workflows._READ_TOOLS) | {"bash", "write_file", "mcp__blue__get-timeline", "mcp__blue__create-post"}
    policy = {
        "known_tools": tools, "allowed_tools": tools, "memory_access": "write", "skill_access": "all",
        "skill_names": set(), "model_access": "all", "allowed_models": set(), "allowed_mcp_servers": ["*"],
        "private_vault_access": False, "delegation_policy": "explicit", "max_parallel_workers": 3,
        "approval_mode": "ask_risky",
    }
    monkeypatch.setattr(agent_loadouts, "caller_policy", lambda sid, owner: copy.deepcopy(policy))
    monkeypatch.setattr(workflows, "_mcp_catalog", lambda: ({
        "mcp__blue__get-timeline": {"server_id": "blue", "name": "get-timeline"},
        "mcp__blue__create-post": {"server_id": "blue", "name": "create-post"},
    }, {"mcp__blue__create-post"}))
    observed = []
    async def headless(sess, messages, **kwargs):
        observed.append((sess.name, messages, kwargs))
        await asyncio.sleep(0)
        return json.dumps({"findings": [sess.name], "evidence": [{"url": "https://source.test"}]}), [
            {"tool": tool, "exit_code": 0} for tool in settings[sess.id]["enabled_tools"] if tool != "manage_skills"]
    monkeypatch.setattr(headless_agent, "run_headless", headless)
    agent_runs._EXTERNAL.clear()
    yield SimpleNamespace(parent=parent, sessions=sessions, settings=settings, policy=policy, observed=observed)
    activity._reset_for_tests()
    agent_runs._EXTERNAL.clear()


def request(**kw):
    return {"action": "start", "task": "Research the market and draft 10 posts", "specialists": [
        {"name": "Buyers", "task": "Map buyer problems", "tools": ["web_search"]},
        {"name": "Competitors", "task": "Compare alternatives", "tools": ["web_fetch"]},
        {"name": "Content", "task": "Research audience themes", "tools": ["mcp__blue__get-timeline"],
         "skills": ["marketing-content-workflow", "agent-handoff-contracts"]},
    ], "synthesis": {"name": "Synthesis", "task": "Produce the market map and 10 posts with evidence"}, **kw}


async def start_and_wait(args=None):
    result = await workflows.start(session_id="parent", owner="alice", args=args or request(), delegation_authorized=True)
    return await workflows.inspect(workflow_id=result["workflow_id"], session_id="parent", owner="alice", action="wait", wait_seconds=3)


async def test_three_research_children_then_synthesis_real_worker_lifecycle(runtime):
    result = await start_and_wait()
    assert result["status"] == "completed"
    assert result["launched_agents"] == result["requested_agents"] == 4
    assert result["synthesis_status"] == "completed"
    assert [name for name, _, _ in runtime.observed] == ["Buyers", "Competitors", "Content", "Synthesis"]
    children = result["children"]
    assert len({row["run_id"] for row in children}) == 4
    assert all(row["handoff"]["artifact_id"] == row["run_id"] for row in children)
    assert {row["tool_calls"][0]["tool"] for row in children[:3]} == {"web_search", "web_fetch", "mcp__blue__get-timeline"}
    synthesis_prompt = runtime.observed[-1][1][-1]["content"]
    assert "untrusted research artifacts" in synthesis_prompt
    assert all(row["run_id"] in synthesis_prompt for row in children[:3])
    content_prompt = runtime.observed[2][1][-1]["content"]
    assert '"action": "view"' in content_prompt and "Loading instructions is not executing" in content_prompt
    saved = runtime.settings[children[2]["session_id"]]
    assert saved["workflow_readonly"] is True
    assert saved["delegation_policy"] == "never"
    assert saved["max_parallel_workers"] == 0
    assert saved["allowed_mcp_servers"] == ["blue"]
    assert saved["tool_access"] == "selected"
    assert "mcp__blue__create-post" in saved["disabled_tools"]
    assert len([msg for msg in runtime.parent.history if (msg.metadata or {}).get("workflow_id")]) == 1
    manifest = runtime.settings["parent"]["agent_workflows"][result["workflow_id"]]
    assert manifest["status"] == "completed"
    assert not workflows._TASKS and not agent_control._WORKERS


async def test_capacity_one_queues_instead_of_raising_parent_limit(runtime, monkeypatch):
    from src import headless_agent
    runtime.policy["max_parallel_workers"] = 1
    active = maximum = 0
    async def headless(sess, *args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "Research findings", [{"tool": name, "exit_code": 0} for name in runtime.settings[sess.id]["enabled_tools"]]
    monkeypatch.setattr(headless_agent, "run_headless", headless)
    result = await start_and_wait()
    assert result["status"] == "completed" and maximum == 1
    assert runtime.policy["max_parallel_workers"] == 1


@pytest.mark.parametrize("tool", ["bash", "write_file", "mcp__blue__create-post", "mcp__missing__get"])
async def test_unsafe_or_unknown_tools_launch_nothing(runtime, tool):
    args = request(specialists=[{"name": "Unsafe", "task": "Read", "tools": [tool]}])
    with pytest.raises(ValueError):
        await start_and_wait(args)
    assert not agent_control._WORKERS and len(runtime.sessions) == 1


async def test_parent_ownership_authorization_and_private_policy(runtime):
    with pytest.raises(LookupError):
        await workflows.start(session_id="parent", owner="bob", args=request(), delegation_authorized=True)
    with pytest.raises(ValueError, match="explicitly"):
        await workflows.start(session_id="parent", owner="alice", args=request(), delegation_authorized=False)
    runtime.policy["private_vault_access"] = True
    result = await start_and_wait()
    assert all(not runtime.settings[row["session_id"]]["private_vault_access"] for row in result["children"])
    with pytest.raises(LookupError):
        await workflows.inspect(workflow_id=result["workflow_id"], session_id="parent", owner="bob")


async def test_denied_mcp_and_model_and_skill_launch_nothing(runtime):
    runtime.policy["allowed_mcp_servers"] = []
    with pytest.raises(ValueError, match="MCP server denied"):
        await start_and_wait()
    runtime.policy["allowed_mcp_servers"] = ["*"]
    runtime.policy["model_access"] = "current"
    args = request()
    args["specialists"][0]["model"] = "other"
    with pytest.raises(ValueError, match="outside"):
        await start_and_wait(args)
    runtime.policy["skill_access"] = "none"
    with pytest.raises(ValueError, match="Skills are disabled"):
        await start_and_wait()
    assert len(runtime.sessions) == 1


async def test_partial_failure_remains_visible_to_synthesis(runtime, monkeypatch):
    from src import headless_agent
    async def headless(sess, messages, **kwargs):
        if sess.name == "Competitors":
            raise RuntimeError("provider failed")
        runtime.observed.append((sess.name, messages, kwargs))
        return "Findings", [{"tool": name, "exit_code": 0} for name in runtime.settings[sess.id]["enabled_tools"]]
    monkeypatch.setattr(headless_agent, "run_headless", headless)
    result = await start_and_wait()
    assert result["status"] == "partial" and result["synthesis_status"] == "completed"
    assert result["children"][1]["status"] == "failed"
    assert '"status": "failed"' in runtime.observed[-1][1][-1]["content"]
    assert result["failures"]


async def test_optional_retry_is_bounded_and_retains_failed_attempt_trace(runtime, monkeypatch):
    from src import headless_agent
    counts = {}
    async def headless(sess, messages, **kwargs):
        counts[sess.name] = counts.get(sess.name, 0) + 1
        if sess.name == "Buyers" and counts[sess.name] == 1:
            raise RuntimeError("transient model failure")
        return "Findings", [{"tool": name, "exit_code": 0} for name in runtime.settings[sess.id]["enabled_tools"]]
    monkeypatch.setattr(headless_agent, "run_headless", headless)
    result = await start_and_wait(request(retries=1))
    assert result["status"] == "completed"
    assert result["children"][0]["attempt"] == 2
    assert [row["status"] for row in result["children"][0]["attempts"]] == ["failed", "completed"]
    assert counts["Buyers"] == 2


async def test_cancel_and_timeout_stop_real_children(runtime, monkeypatch):
    from src import headless_agent
    async def hanging(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(headless_agent, "run_headless", hanging)
    first = await workflows.start(session_id="parent", owner="alice", args=request(), delegation_authorized=True)
    assert agent_control.live_children("parent") == 3
    result = await workflows.inspect(workflow_id=first["workflow_id"], session_id="parent", owner="alice", action="cancel")
    assert result["status"] == "cancelled" and not agent_control._WORKERS
    now = [0]
    def tick():
        now[0] += 20
        return now[0]
    monkeypatch.setattr(workflows, "time", SimpleNamespace(time=lambda: 123, monotonic=tick))
    result = await start_and_wait(request(timeout_seconds=30))
    assert result["status"] == "timed_out" and not agent_control._WORKERS
    assert any(row["status"] == "timed_out" for row in result["children"])


async def test_zero_launches_and_policy_save_failure_are_not_success(runtime, monkeypatch):
    import core.database as db
    original = db.update_session_settings
    monkeypatch.setattr(db, "update_session_settings", lambda sid, patch: original(sid, patch) if sid == "parent" else None)
    result = await start_and_wait()
    assert result["status"] == "failed" and result["launched_agents"] == 0
    assert result["synthesis_status"] == "not_started"
    assert not agent_control._WORKERS


async def test_restart_reports_interrupted_without_replay(runtime):
    runtime.settings["parent"] = {"agent_workflows": {"gone": {
        "workflow_id": "gone", "parent_session": "parent", "owner": "alice", "status": "running",
        "children": [], "failures": [],
    }}}
    result = await workflows.inspect(workflow_id="gone", session_id="parent", owner="alice")
    assert result["status"] == "interrupted" and result["launched_agents"] == 0
    assert not workflows._TASKS
    assert runtime.settings["parent"]["agent_workflows"]["gone"]["status"] == "interrupted"


async def test_tool_wrapper_errors_are_truthful(runtime):
    tool = OrchestrateAgentsTool()
    result = await tool.execute(json.dumps(request()), {"session_id": "parent", "owner": "alice", "delegation_authorized": False})
    assert result["exit_code"] == 1 and result["launched_agents"] == 0
    assert "explicitly" in result["error"]


async def test_empty_worker_output_stays_failed_in_public_result(runtime, monkeypatch):
    from src import headless_agent
    async def empty(*args, **kwargs):
        return "", []
    monkeypatch.setattr(headless_agent, "run_headless", empty)
    result = await start_and_wait()
    assert result["status"] == "failed"
    assert all(row["status"] == "failed" for row in result["children"][:3])
    assert result["synthesis_status"] == "not_started"


async def test_handoff_output_is_bounded_without_hiding_synthesis(runtime, monkeypatch):
    from src import headless_agent
    async def large(sess, messages, **kwargs):
        return ("FINAL SYNTHESIS\n" if sess.name == "Synthesis" else "EVIDENCE\n") + "x" * 30000, [
            {"tool": name, "exit_code": 0} for name in runtime.settings[sess.id]["enabled_tools"]]
    monkeypatch.setattr(headless_agent, "run_headless", large)
    result = await start_and_wait()
    assert all(len(row.get("result", "")) <= 1800 for row in result["children"][:3])
    assert len(result["children"][-1]["result"]) == 20000
    assert all(row["result_truncated"] for row in result["children"])
    assert all("content" not in row["handoff"] for row in result["children"])
    report = workflows.render_result(result)
    assert report.index("FINAL SYNTHESIS") < report.index("Execution trace")
    assert len(report) < 25000
    assert len(runtime.parent.history[-1].content) < 25000
    events = activity.history("parent", limit=100)
    assert any(event["kind"] == "message" and (event.get("data") or {}).get("workflow_id") == result["workflow_id"] for event in events)


def test_disconnected_mcp_catalog_cannot_be_attached(monkeypatch):
    import src.tool_utils as utils
    import src.agent_loop as loop
    manager = SimpleNamespace(
        get_all_tools=lambda disabled: [{"qualified_name": "mcp__gone__get", "server_id": "gone"},
                                        {"qualified_name": "mcp__live__get", "server_id": "live"}],
        get_server_status=lambda sid: {"status": "connected" if sid == "live" else "error"},
        plan_mode_blocked_mcp=lambda: ({}, set()),
    )
    monkeypatch.setattr(utils, "get_mcp_manager", lambda: manager)
    monkeypatch.setattr(loop, "_load_mcp_disabled_map", lambda: {})
    catalog, _ = workflows._mcp_catalog()
    assert set(catalog) == {"mcp__live__get"}


async def test_control_room_stop_uses_workflow_controller(runtime, monkeypatch):
    from src import headless_agent
    async def hanging(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(headless_agent, "run_headless", hanging)
    result = await workflows.start(session_id="parent", owner="alice", args=request(), delegation_authorized=True)
    stopped = await agent_control.stop_run(result["workflow_id"])
    assert stopped == {"stopped": True, "how": "workflow", "status": "cancelled"}
    assert not agent_control._WORKERS


async def test_cancellation_cleanup_is_bounded(runtime, monkeypatch):
    from src import headless_agent
    release = asyncio.Event()
    async def stubborn(*args, **kwargs):
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return "Late result", []
    monkeypatch.setattr(headless_agent, "run_headless", stubborn)
    monkeypatch.setattr(workflows, "STOP_GRACE_SECONDS", 0.01)
    result = await workflows.start(session_id="parent", owner="alice", args=request(), delegation_authorized=True)
    await asyncio.sleep(0)
    result = await workflows.inspect(workflow_id=result["workflow_id"], session_id="parent", owner="alice", action="cancel")
    assert result["status"] == "cancelled"
    assert any(row["status"] == "stopping" for row in result["children"])
    assert any("still stopping" in row.get("error", "") for row in result["failures"])
    pending = list(agent_control._WORKERS.values())
    release.set()
    await asyncio.gather(*pending)


async def test_web_only_content_cannot_claim_bound_bluesky_research_completed(runtime, monkeypatch):
    from src import headless_agent
    async def skipped_mcp(sess, messages, **kwargs):
        return "I researched everything", [{"tool": "web_search", "exit_code": 0}]
    monkeypatch.setattr(headless_agent, "run_headless", skipped_mcp)
    args = request()
    args["specialists"][2]["tools"].append("web_search")
    result = await start_and_wait(args)
    content = result["children"][2]
    assert content["status"] == "incomplete"
    assert "MCP research on blue" in content["reason"]
    assert result["status"] == "partial"


@pytest.mark.parametrize("events", [[{"tool": "manage_skills", "exit_code": 0}],
                                   [{"tool": "web_search", "exit_code": 1}],
                                   [{"tool": "web_search", "error": "failed"}]])
def test_skill_loading_or_failed_calls_are_not_research_evidence(events):
    child = {"stage": "research", "tools": ["web_search", "manage_skills"]}
    assert workflows._missing_evidence(child, {"tool_calls": events})
    assert not workflows._missing_evidence({"stage": "synthesis", "tools": []}, {"tool_calls": []})


async def test_worker_cancelled_before_first_instruction_releases_capacity(runtime):
    record = await agent_control.launch_worker(owner="alice", task="Research", parent_session="parent",
                                               inline_profile={"name": "Probe", "tool_access": "none"}, handoff=False)
    task = agent_control._WORKERS[record["run_id"]]
    task.cancel()  # deliberately no event-loop yield after create_task
    await asyncio.gather(task, return_exceptions=True)
    assert record["run_id"] not in agent_control._WORKERS
    assert activity.get_run(record["run_id"])["status"] == "cancelled"
    assert agent_control.live_children("parent") == 0
    result = agent_control.collect_worker_result(record["run_id"], owner="alice", session_id=record["session_id"])
    assert result["status"] == "cancelled" and result["result"] == ""


async def test_post_launch_manifest_failure_does_not_leak_unstarted_workers(runtime, monkeypatch):
    import core.database as db
    original = db.update_session_settings
    writes = [0]
    def fail_after_initial_manifest(sid, patch):
        if sid == "parent":
            writes[0] += 1
            if writes[0] == 2:
                return None
        return original(sid, patch)
    monkeypatch.setattr(db, "update_session_settings", fail_after_initial_manifest)
    result = await start_and_wait()
    assert result["status"] == "failed"
    assert not agent_control._WORKERS and agent_control.live_children("parent") == 0
    assert all(activity.get_run(row["run_id"])["status"] == "cancelled" for row in result["children"] if row.get("run_id"))


async def test_artifacts_survive_bounded_activity_registry_eviction(runtime):
    result = await start_and_wait()
    for child in result["children"]:
        activity._runs.pop(child["run_id"])
    restored = await workflows.inspect(workflow_id=result["workflow_id"], session_id="parent", owner="alice")
    assert restored["status"] == "completed"
    assert all(row["result"] and row["status"] == "completed" for row in restored["children"])
    first, second = restored["children"][:2]
    with pytest.raises(LookupError):
        agent_control.collect_worker_result(first["run_id"], owner="alice", session_id=second["session_id"])
    with pytest.raises(LookupError):
        agent_control.collect_worker_result(first["run_id"], owner="bob", session_id=first["session_id"])


async def test_new_workflow_prunes_restarted_manifests_to_bound(runtime):
    rows = {}
    for index in range(workflows.MAX_WORKFLOWS):
        wid = f"stale-{index}"
        rows[wid] = {"workflow_id": wid, "parent_session": "parent", "owner": "alice", "status": "running",
                     "children": [{"name": "Never started", "status": "queued"}], "failures": [], "started_at": index}
    runtime.settings["parent"] = {"agent_workflows": rows}
    result = await start_and_wait()
    persisted = runtime.settings["parent"]["agent_workflows"]
    assert len(persisted) == workflows.MAX_WORKFLOWS
    assert persisted[result["workflow_id"]]["status"] == "completed"
    recovered = [row for wid, row in persisted.items() if wid != result["workflow_id"]]
    assert all(row["status"] == "interrupted" for row in recovered)
    assert all(row["children"][0]["status"] == "not_started" for row in recovered)


def test_public_child_does_not_finish_before_controller_evidence_validation(monkeypatch):
    monkeypatch.setattr(agent_control, "collect_worker_result", lambda *args, **kwargs: {
        "status": "completed", "result": "Claimed research", "tool_calls": []})
    rec = {"workflow_id": "pending", "owner": "alice", "status": "running", "failures": [],
           "children": [{"name": "Research", "stage": "research", "status": "running", "run_id": "r1",
                         "session_id": "child", "tools": ["web_search"]}]}
    assert workflows._public(rec)["children"][0]["status"] == "running"


@pytest.mark.parametrize("failure_site", ["snapshot", "activity"])
async def test_controller_registries_cleanup_even_when_final_telemetry_raises(runtime, monkeypatch, failure_site):
    original_public = workflows._public
    original_finished = activity.run_finished
    if failure_site == "snapshot":
        def broken_snapshot(rec, **kwargs):
            if rec["status"] != "running":
                raise RuntimeError("Final snapshot unavailable")
            return original_public(rec, **kwargs)
        monkeypatch.setattr(workflows, "_public", broken_snapshot)
    else:
        def broken_finished(session_id, source, *args, **kwargs):
            if source == "pipeline":
                raise RuntimeError("Activity store unavailable")
            return original_finished(session_id, source, *args, **kwargs)
        monkeypatch.setattr(activity, "run_finished", broken_finished)
    result = await workflows.start(session_id="parent", owner="alice", args=request(), delegation_authorized=True)
    task = workflows._TASKS[result["workflow_id"]]
    await task
    assert not workflows._TASKS and not workflows._LIVE and not agent_control._WORKERS
