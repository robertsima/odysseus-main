"""Real agent-loop routing/stream tests; model and tool execution stay offline."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.agent_loop as agent_loop
from src.workflow_claims import incomplete_execution_notice, record_execution


USER_REQUEST = "use appropriately scoped agents and MCP tools to research Umni"
VERIFIED_ANSWER = "The verified specialist handoffs identify buyers, competitors and audience themes; here is the final synthesis."


def _no_security_context():
    # Looked up at call time: other tests reload src.tool_execution, which
    # replaces the sentinel execute_tool_block compares by identity.
    import src.tool_execution as tool_execution

    return tool_execution.NO_TOOL_SECURITY_CONTEXT


@pytest.fixture
def loop_runtime(monkeypatch, tmp_path):
    import core.database as database
    import src.model_context as model_context
    from src import agent_activity, agent_control, constants

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    agent_activity._reset_for_tests()
    settings = {}
    monkeypatch.setattr(database, "get_session_settings", lambda sid, **kwargs: copy.deepcopy(settings))
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(model_context, "budget_context_for_model", lambda *args, **kwargs: 400_000)
    monkeypatch.setattr(agent_loop, "_build_system_prompt", lambda messages, *args, **kwargs: (list(messages), []))
    monkeypatch.setattr(agent_control, "pending_steer", lambda sid, **kwargs: [])
    monkeypatch.setattr(agent_control, "drain_steer_records", lambda *args, **kwargs: [])
    requests, executions = [], []

    async def run(rounds, *, tool_results=None, request=USER_REQUEST, relevant_tools=None, max_rounds=4, messages=None):
        tool_results = list(tool_results or [])

        async def fake_stream(candidates, messages, **kwargs):
            requests.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(kwargs.get("tools") or [])})
            event = rounds[min(len(requests) - 1, len(rounds) - 1)]
            if isinstance(event, str):
                # Multi-chunk output must not leak unverified completion prose.
                for start in range(0, len(event), 53):
                    yield "data: " + json.dumps({"delta": event[start:start + 53]}) + "\n\n"
            else:
                yield "data: " + json.dumps(event) + "\n\n"
            yield "data: [DONE]\n\n"

        async def fake_execute(block, **kwargs):
            executions.append({"tool": block.tool_type, "arguments": block.content, **kwargs})
            result = copy.deepcopy(tool_results[len(executions) - 1])
            result.setdefault("output", json.dumps(result))
            return block.tool_type, result

        monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
        monkeypatch.setattr(agent_loop, "execute_tool_block", fake_execute)
        chunks = [chunk async for chunk in agent_loop.stream_agent_loop(
            "http://unused/v1", "gpt-5-test", messages or [{"role": "user", "content": request}],
            relevant_tools=relevant_tools or {"web_search"}, max_rounds=max_rounds,
            session_id="workflow-routing-test",
        )]
        return [json.loads(chunk[6:]) for chunk in chunks
                if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]")]

    yield SimpleNamespace(run=run, requests=requests, executions=executions, settings=settings)
    agent_activity._reset_for_tests()


def visible_text(events):
    return "".join(event.get("delta", "") for event in events if not event.get("thinking"))


@pytest.mark.asyncio
async def test_readonly_specialist_does_not_attempt_to_orchestrate_its_own_prompt(loop_runtime):
    loop_runtime.settings.update(workflow_readonly=True, delegation_policy="never")
    events = await loop_runtime.run([VERIFIED_ANSWER])

    assert len(loop_runtime.requests) == 1
    assert loop_runtime.executions == []
    assert visible_text(events) == VERIFIED_ANSWER


@pytest.mark.asyncio
async def test_manage_skills_view_returns_explicit_not_run_metadata_without_launch(monkeypatch, tmp_path):
    from services.memory.skills import SkillsManager
    from src import agent_workflows, constants
    from src.tools.system import do_manage_skills

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(SkillsManager, "read_skill_md", lambda self, name, owner=None: "# Research workflow\nLaunch three research agents and collect handoffs.")
    launch = AsyncMock()
    monkeypatch.setattr(agent_workflows, "start", launch)

    result = await do_manage_skills(json.dumps({"action": "view", "name": "launch-structured-marketing-research"}), owner="alice")

    assert result["execution"]["state"] == "loaded_not_run"
    assert result["execution"]["loaded_skills"] == ["launch-structured-marketing-research"]
    assert result["execution"]["next_tool_calls"] == [
        {"tool": "manage_agent_loadout", "arguments": {"action": "capabilities", "detail": True}}
    ]
    assert "No agents or workflow steps have run" in result["results"]
    launch.assert_not_awaited()


def test_skills_and_generic_deep_research_never_create_agent_execution_receipts():
    receipts = {}
    record_execution(receipts, "manage_skills", {"execution": {"state": "loaded_not_run"}, "exit_code": 0})
    record_execution(receipts, "trigger_research", {"session_id": "research", "status": "completed", "exit_code": 0})

    assert receipts == {}
    assert "workflow was not run" in incomplete_execution_notice(receipts)


def test_single_worker_launch_is_evidence_of_launch_not_completed_research():
    receipts = {}
    record_execution(receipts, "manage_agent_loadout", {"session_id": "child", "run_id": "worker-run", "exit_code": 0})

    assert receipts["worker-run"]["launched_agents"] == 1
    assert "workflow is not complete" in incomplete_execution_notice(receipts)


@pytest.mark.asyncio
async def test_execution_fails_closed_when_session_policy_cannot_be_loaded(monkeypatch):
    import core.database as database
    from src import tool_execution

    def unavailable(_sid, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(database, "get_session_settings", unavailable)
    execution = AsyncMock()
    monkeypatch.setattr(tool_execution, "_direct_fallback", execution)
    block = SimpleNamespace(tool_type="read_file", content='{"path":"notes.md"}')
    _, result = await tool_execution.execute_tool_block(block, session_id="restricted-child", disabled_tools=set(), security_context=_no_security_context())
    assert result["blocked_reason"] == "session_policy_unavailable"
    assert result["exit_code"] == 1
    execution.assert_not_awaited()


def test_strict_database_policy_read_propagates_real_storage_errors(monkeypatch):
    from contextlib import contextmanager
    import core.database as database

    @contextmanager
    def unavailable():
        raise RuntimeError("database unavailable")
        yield

    monkeypatch.setattr(database, "get_db_session", unavailable)
    assert database.get_session_settings("child") == {}  # legacy display callers
    with pytest.raises(RuntimeError, match="database unavailable"):
        database.get_session_settings("child", strict=True)
