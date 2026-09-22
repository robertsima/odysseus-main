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
    with pytest.raises(ValueError, match="not been authorized"):
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
    result = await start_and_wait(request(allow_partial_synthesis=True))
    assert result["status"] == "partial" and result["synthesis_status"] == "completed"
    assert result["children"][1]["status"] == "failed"
    assert '"status": "failed"' in runtime.observed[-1][1][-1]["content"]
    assert result["failures"]
    assert result["exit_code"] == 2 and result["degraded"] is True and result["ok"] is False
    assert result["research_completed"] == result["usable_handoffs"] == 2
    assert result["research_failed"] == 1
    assert "handoff" not in result["children"][1]
    assert "PARTIAL SYNTHESIS" in runtime.observed[-1][1][-1]["content"]


async def test_required_research_failure_blocks_synthesis_by_default(runtime, monkeypatch):
    from src import headless_agent
    async def headless(sess, messages, **kwargs):
        if sess.name == "Competitors":
            raise RuntimeError("provider failed")
        return "Findings", [{"tool": name, "exit_code": 0}
                             for name in runtime.settings[sess.id]["enabled_tools"]]
    monkeypatch.setattr(headless_agent, "run_headless", headless)
    result = await start_and_wait()
    assert result["status"] == "partial"
    assert result["synthesis_status"] == "not_started"
    assert result["exit_code"] == 2
    synthesis = next(row for row in result["children"] if row["stage"] == "synthesis")
    assert "Required research did not produce usable evidence" in synthesis["reason"]


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
    assert "transient model failure" in result["children"][0]["attempts"][0]["reason"]
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
    assert "not been authorized" in result["error"]


@pytest.mark.parametrize("action", [None, "status"])
async def test_tool_recovers_start_shaped_calls_with_missing_or_stale_action(runtime, action):
    args = request()
    if action is None:
        args.pop("action")
    else:
        args["action"] = action
    tool = OrchestrateAgentsTool()
    result = await tool.execute(json.dumps(args), {
        "session_id": "parent", "owner": "alice", "delegation_authorized": True,
    })
    assert result["action"] == "start"
    assert result["workflow_id"].startswith("workflow-")
    completed = await tool.execute(json.dumps({"action": "wait", "wait_seconds": 3}), {
        "session_id": "parent", "owner": "alice", "delegation_authorized": True,
    })
    assert completed["workflow_id"] == result["workflow_id"]
    assert completed["status"] == "completed"


async def test_tool_missing_status_id_explains_that_no_workflow_exists(runtime):
    result = await OrchestrateAgentsTool().execute('{"action":"status"}', {
        "session_id": "parent", "owner": "alice",
    })
    assert result["exit_code"] == 1
    assert "No workflow has been started" in result["error"]
    assert "action=start" in result["error"]


def test_missing_followup_id_does_not_guess_between_running_workflows(runtime):
    runtime.settings["parent"] = {"agent_workflows": {
        "workflow-one": {"workflow_id": "workflow-one", "parent_session": "parent", "owner": "alice",
                         "status": "running", "started_at": 1},
        "workflow-two": {"workflow_id": "workflow-two", "parent_session": "parent", "owner": "alice",
                         "status": "running", "started_at": 2},
    }}
    with pytest.raises(LookupError, match="Multiple workflows are running"):
        workflows.resolve_workflow_id("", "parent", "alice")


async def test_five_specialists_are_bounded_by_parent_capacity_not_rejected(runtime):
    specialists = [
        {"name": f"Workstream {index}", "task": f"Research area {index}", "tools": ["web_search"]}
        for index in range(5)
    ]
    result = await start_and_wait(request(specialists=specialists, synthesis=None))
    assert result["status"] == "completed"
    assert result["requested_agents"] == result["launched_agents"] == 5


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


async def test_workspace_orientation_tool_is_a_valid_research_binding(runtime):
    args = request(specialists=[{"name": "Repo", "task": "Read the checkout", "tools": ["get_workspace", "grep"]}],
                   synthesis=None)
    result = await start_and_wait(args)
    assert result["status"] == "completed"
    assert runtime.settings[result["children"][0]["session_id"]]["enabled_tools"] == ["get_workspace", "grep"]


async def test_rejected_binding_names_every_supported_tool_so_the_retry_can_be_correct(runtime):
    args = request(specialists=[{"name": "Unsafe", "task": "Read", "tools": ["todowrite", "bash"]}])
    with pytest.raises(ValueError) as excinfo:
        await workflows.start(session_id="parent", owner="alice", args=args, delegation_authorized=True)
    message = str(excinfo.value)
    assert "todowrite, bash are not supported read-only research tools" in message
    assert all(tool in message for tool in workflows._READ_TOOLS)
    assert "mcp__serverId__tool" in message
    assert not agent_control._WORKERS and len(runtime.sessions) == 1


def test_research_bindings_track_the_harness_read_only_classification():
    """A specialist must not be read-only AND arbitrarily narrower than its parent.

    The accepted set used to be a hand-maintained list of 13 names, so a
    specialist could be refused `get_workspace` while the chat that started it
    used it freely. It now derives from the harness's own read-only
    classification, so a newly classified read-only tool is available to
    research without a second list to remember.
    """
    from src.tool_security import PLAN_MODE_READONLY_TOOLS, _PLAN_MODE_KNOWN_MUTATORS

    assert PLAN_MODE_READONLY_TOOLS <= workflows._READ_TOOLS
    # `manage_skills` is the one deliberate exception: a specialist loads the
    # procedures it was given with it, and `_prepare` attaches it whenever
    # skills are requested. Nothing else that can change the world is here.
    assert (workflows._READ_TOOLS & _PLAN_MODE_KNOWN_MUTATORS) == {"manage_skills"}


def test_schema_points_at_the_rejection_rather_than_a_stale_list():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    schema = next(entry["function"] for entry in FUNCTION_TOOL_SCHEMAS
                  if entry.get("function", {}).get("name") == "orchestrate_agents")
    described = schema["parameters"]["properties"]["specialists"]["items"]["properties"]["tools"]["description"]
    assert "read-only" in described and "rejection lists the full supported set" in described


async def test_preflight_reports_model_bindings_and_mcp_health_per_agent(runtime, monkeypatch):
    monkeypatch.setattr(workflows, "mcp_server_health", lambda: {
        "blue": {"server_id": "blue", "server_name": "bluesky-mcp", "status": "connected", "error": None}})
    result = await workflows.start(session_id="parent", owner="alice", args=request(), delegation_authorized=True)
    rows = {row["name"]: row for row in result["preflight"]}
    assert set(rows) == {"Buyers", "Competitors", "Content", "Synthesis"}
    assert rows["Buyers"]["tools"] == ["web_search"] and rows["Buyers"]["model"] == "inherit"
    assert rows["Content"]["mcp_servers"] == [
        {"server_id": "blue", "server_name": "bluesky-mcp", "status": "connected", "error": None}]
    assert all(row["ready"] for row in result["preflight"])
    assert result["preflight_blocked"] == []
    await workflows._TASKS[result["workflow_id"]]


async def test_disconnected_mcp_server_is_a_named_preflight_blocker(runtime, monkeypatch):
    monkeypatch.setattr(workflows, "mcp_server_health", lambda: {
        "blue": {"server_id": "blue", "server_name": "bluesky-mcp", "status": "error",
                 "error": "Connection closed"}})
    result = await workflows.start(session_id="parent", owner="alice", args=request(), delegation_authorized=True)
    assert result["preflight_blocked"] == ["Content"]
    blocked = next(row for row in result["preflight"] if row["name"] == "Content")
    assert blocked["blockers"] == ["MCP server bluesky-mcp is error (Connection closed)"]
    await workflows._TASKS[result["workflow_id"]]
    final = await workflows.inspect(workflow_id=result["workflow_id"], session_id="parent", owner="alice")
    assert "Preflight blockers" in workflows.render_result(final)


async def test_expired_provider_credentials_launch_nothing_at_all(runtime, monkeypatch):
    monkeypatch.setattr(workflows, "_endpoint_auth_state",
                        lambda url, owner: ("expired", "the provider auth session returned no usable access token"))
    with pytest.raises(RuntimeError, match="expired or were rejected"):
        await workflows.start(session_id="parent", owner="alice", args=request(), delegation_authorized=True)
    assert not agent_control._WORKERS and len(runtime.sessions) == 1
    assert not runtime.settings.get("parent", {}).get("agent_workflows")


@pytest.mark.parametrize("reason,retried", [
    ("Upstream model request failed with HTTP 401: credentials expired or were rejected", False),
    ("Upstream model request failed with HTTP 503: upstream temporarily unavailable", True),
])
async def test_authentication_failures_are_not_retried(runtime, monkeypatch, reason, retried):
    from src import headless_agent
    attempts = []

    async def headless(sess, *args, **kwargs):
        attempts.append(sess.id)
        raise RuntimeError(reason)

    monkeypatch.setattr(headless_agent, "run_headless", headless)
    args = request(specialists=[{"name": "Buyers", "task": "Map buyers", "tools": ["web_search"]}],
                   synthesis=None, retries=1)
    result = await start_and_wait(args)
    assert result["status"] == "failed"
    assert len(attempts) == (2 if retried else 1)
    if not retried:
        assert any("Not retried" in str(failure.get("error", "")) for failure in result["failures"])


# ── the run record ───────────────────────────────────────────────────────────
# "Workflow completed" answered none of: what was asked, who ran, where the
# answer came from, what was checked, what is still open -- without reading
# every child chat.

async def test_run_record_accounts_for_the_run_and_outlives_the_controller(runtime):
    result = await start_and_wait()
    record = result["record"]
    assert record["run_id"] == result["workflow_id"] and record["status"] == "completed"
    assert record["objective"] == "Research the market and draft 10 posts"
    assert [a["name"] for a in record["selected_agents"]] == ["Buyers", "Competitors", "Content", "Synthesis"]
    assert record["selected_agents"][0]["tools"] == ["web_search"] and record["selected_agents"][0]["required"]
    assert record["result_summary"]["kind"] == "synthesis" and record["result_summary"]["agent"] == "Synthesis"
    assert record["result_summary"]["verified"] is False and record["result_summary"]["provisional"] is False
    assert "Synthesis" in record["result_summary"]["excerpt"]
    # Raw specialist output is listed apart from the summary, as pointers.
    assert [o["agent"] for o in record["raw_outputs"]] == ["Buyers", "Competitors", "Content"]
    assert all(o["chars"] > 0 and o["chat"].startswith("#session-") for o in record["raw_outputs"])
    assert len(record["artifacts"]) == 4 and all(a["usable"] for a in record["artifacts"])
    assert record["changed_files"] == []
    checks = {(v["agent"], v["check"].split(":")[0]) for v in record["verification"]}
    assert ("Synthesis", "preflight") in checks and ("Buyers", "evidence") in checks
    assert all(v["passed"] for v in record["verification"]) and record["unresolved"] == []
    assert record["timing"]["timed_out"] is False and record["timing"]["finished_at"]
    # The controller is gone; the record is read back from the manifest.
    assert result["workflow_id"] not in workflows._LIVE
    stored = runtime.settings["parent"]["agent_workflows"][result["workflow_id"]]["record"]
    assert stored["status"] == "completed" and stored["result_summary"]["kind"] == "synthesis"
    later = await workflows.inspect(workflow_id=result["workflow_id"], session_id="parent", owner="alice")
    assert later["record"] == stored
    assert "Run record: result from synthesis 'Synthesis'" in runtime.parent.history[-1].content


async def test_run_record_names_failed_checks_and_open_questions(runtime, monkeypatch):
    from src import headless_agent

    async def headless(sess, messages, **kwargs):
        if sess.name == "Competitors":
            return "", []
        return json.dumps({"findings": [sess.name], "evidence": [{"url": "https://s.test"}],
                           "open_questions": [f"{sess.name}: pricing unknown"]}), [
            {"tool": tool, "exit_code": 0} for tool in runtime.settings[sess.id]["enabled_tools"]
            if tool != "manage_skills"]

    monkeypatch.setattr(headless_agent, "run_headless", headless)
    result = await start_and_wait()
    record = result["record"]
    assert result["status"] == "partial" and record["status"] == "partial"
    assert record["result_summary"] == {"kind": "none", "agent": None, "status": None, "excerpt": "",
                                        "excerpt_truncated": False, "provisional": False, "verified": False}
    failed = [v for v in record["verification"] if not v["passed"]]
    assert [(v["agent"], v["detail"]) for v in failed] == [("Competitors", "Worker returned no result")]
    issues = {(i.get("agent"), i["issue"], i.get("from")) for i in record["unresolved"]}
    assert ("Competitors", "research branch failed: Worker returned no result", None) in issues
    assert ("Buyers", "Buyers: pricing unknown", "handoff") in issues
    assert any(agent == "Synthesis" and issue.startswith("synthesis branch not_started") for agent, issue, _ in issues)
    report = workflows.render_result(result)
    assert "verification 7 check(s), 1 failed" in report
    assert "Failed check — Competitors: evidence" in report
    assert "Unresolved — Buyers: Buyers: pricing unknown" in report
    # The obligation reaches the parent on both surfaces it reads: the tool
    # result mid-turn and the hand-off message on its next turn.
    obligation = ("Reporting obligation: this workflow is partial. Tell the user plainly that it is partial: "
                  "failed or unstarted branches — research branch failed: Worker returned no result; "
                  "synthesis branch not_started")
    assert obligation in report
    assert "open questions — Buyers: Buyers: pricing unknown" in report
    assert "do not fill the gaps from memory" in report
    assert obligation in runtime.parent.history[-1].content
    assert not workflows.clean_record(record)


async def test_the_record_and_its_obligation_are_read_before_the_synthesis(runtime, monkeypatch):
    from src import headless_agent

    async def headless(sess, messages, **kwargs):
        if sess.name == "Competitors":
            return "", []
        return ("PROVISIONAL SYNTHESIS\n" if sess.name == "Synthesis" else "EVIDENCE\n") + "x" * 5000, [
            {"tool": tool, "exit_code": 0} for tool in runtime.settings[sess.id]["enabled_tools"]
            if tool != "manage_skills"]

    monkeypatch.setattr(headless_agent, "run_headless", headless)
    result = await start_and_wait(request(allow_partial_synthesis=True))
    record = result["record"]
    assert result["status"] == "partial"
    assert record["result_summary"]["kind"] == "synthesis" and record["result_summary"]["provisional"] is True
    report = workflows.render_result(result)
    assert report.index("Reporting obligation: this workflow is partial and its result is provisional") \
        < report.index("PROVISIONAL SYNTHESIS") < report.index("Execution trace")


async def test_a_clean_run_says_so_without_calling_the_claims_verified(runtime):
    result = await start_and_wait()
    assert workflows.clean_record(result["record"])
    report = workflows.render_result(result)
    assert "Reporting obligation" not in report
    assert "All checks passed. Worker claims are still unverified" in report


async def test_a_one_agent_run_reports_the_specialist_as_the_result_source(runtime):
    result = await start_and_wait(request(
        specialists=[{"name": "Buyers", "task": "Map buyers", "tools": ["web_search"]}], synthesis=None))
    record = result["record"]
    assert record["status"] == "completed"
    assert record["result_summary"]["kind"] == "specialist" and record["result_summary"]["agent"] == "Buyers"
    assert [a["name"] for a in record["selected_agents"]] == ["Buyers"]
    assert "raw specialist output, untrusted" in workflows.render_result(result)


# ── Persisting the final artifact and resuming synthesis (2026-09-22) ──────

def _capture_documents(monkeypatch, created):
    from src.agent_tools import document_tools

    async def fake_create(self, content, ctx):
        created.append((content, ctx))
        return {"action": "create", "doc_id": f"doc-{len(created)}", "title": "t"}

    monkeypatch.setattr(document_tools.CreateDocumentTool, "execute", fake_create)
    monkeypatch.setattr(workflows, "_document_matches", lambda doc_id, owner, text: True)


async def test_parent_persists_one_verified_document_workers_stay_read_only(runtime, monkeypatch):
    created = []
    _capture_documents(monkeypatch, created)
    runtime.policy["allowed_tools"] = set(runtime.policy["allowed_tools"]) | {"create_document"}
    result = await start_and_wait(request(persist_document={"title": "Market map"}))
    assert result["status"] == "completed"
    assert len(created) == 1
    content, ctx = created[0]
    assert ctx == {"session_id": "parent", "owner": "alice"}
    assert "<title>Market map</title>" in content and "Synthesis" in content
    record = result["record"]
    assert record["editor_documents_created"] == [{"document_id": "doc-1", "title": "Market map", "verified": True}]
    assert record["handoff_artifacts"] == 4 and record["repository_files_changed"] == []
    # No worker was given document mutation to make this happen.
    for child in result["children"]:
        assert "create_document" not in (runtime.settings[child["session_id"]].get("enabled_tools") or [])
    assert "editor documents created 1" in workflows.render_record(record)


async def test_persistence_needs_the_parents_own_permission(runtime):
    with pytest.raises(ValueError, match="may not create documents"):
        await workflows.start(session_id="parent", owner="alice", args=request(persist_document=True),
                              delegation_authorized=True)
    assert not agent_control._WORKERS


async def test_no_document_is_claimed_when_synthesis_did_not_complete(runtime, monkeypatch):
    from src import headless_agent
    created = []
    _capture_documents(monkeypatch, created)
    runtime.policy["allowed_tools"] = set(runtime.policy["allowed_tools"]) | {"create_document"}
    original = headless_agent.run_headless

    async def empty_synthesis(sess, messages, **kwargs):
        if sess.name == "Synthesis":
            return "", []
        return await original(sess, messages, **kwargs)

    monkeypatch.setattr(headless_agent, "run_headless", empty_synthesis)
    result = await start_and_wait(request(persist_document=True))
    assert created == []
    persistence = result["record"]["persistence"]
    assert persistence["status"] == "skipped" and not persistence.get("document_id")
    assert "do not say one was created" in workflows.render_record(result["record"])


async def test_resume_reuses_completed_research_and_launches_only_synthesis(runtime, monkeypatch):
    from src import headless_agent
    original = headless_agent.run_headless

    async def hang_synthesis(sess, messages, **kwargs):
        if sess.name == "Synthesis":
            await asyncio.Event().wait()
        return await original(sess, messages, **kwargs)

    monkeypatch.setattr(headless_agent, "run_headless", hang_synthesis)
    first = await workflows.start(session_id="parent", owner="alice", args=request(), delegation_authorized=True)
    for _ in range(50):
        status = await workflows.inspect(workflow_id=first["workflow_id"], session_id="parent", owner="alice")
        if status["synthesis_status"] == "running":
            break
        await asyncio.sleep(0.05)
    cancelled = await workflows.inspect(workflow_id=first["workflow_id"], session_id="parent", owner="alice",
                                        action="cancel")
    assert cancelled["status"] == "cancelled"
    assert cancelled["research_completed"] == 3
    research_runs = {row["name"]: row["run_id"] for row in cancelled["children"] if row["stage"] == "research"}

    monkeypatch.setattr(headless_agent, "run_headless", original)
    launched_before = len(runtime.observed)
    resumed = await OrchestrateAgentsTool().execute(
        json.dumps({"action": "resume", "workflow_id": first["workflow_id"]}),
        {"session_id": "parent", "owner": "alice"},
    )
    assert resumed.get("error") is None, resumed
    done = await workflows.inspect(workflow_id=resumed["workflow_id"], session_id="parent", owner="alice",
                                   action="wait", wait_seconds=3)
    assert done["status"] == "completed"
    assert [name for name, _, _ in runtime.observed[launched_before:]] == ["Synthesis"]
    reused = {row["name"]: row["run_id"] for row in done["children"] if row["stage"] == "research"}
    assert reused == research_runs
    assert done["record"]["resumed_from"] == first["workflow_id"]
    assert sorted(done["record"]["reused_handoffs"]) == sorted(research_runs)
    original_manifest = runtime.settings["parent"]["agent_workflows"][first["workflow_id"]]
    assert original_manifest["status"] == "cancelled"
    assert original_manifest["resumed_by"] == resumed["workflow_id"]


async def test_a_completed_or_running_workflow_cannot_be_resumed(runtime):
    done = await start_and_wait()
    with pytest.raises(ValueError, match="only a finished workflow that did not complete"):
        await workflows.resume(session_id="parent", owner="alice", args={"workflow_id": done["workflow_id"]})
