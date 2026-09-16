"""Capacity checks distinguish starting work from observing existing work."""

from src.tool_execution import _capacity_limited_tool_call, _worker_capacity_result


def test_delegation_inspection_actions_are_never_capacity_blocked():
    for action in ("poll", "get", "cancel", "list", "status", "list_repositories"):
        assert not _capacity_limited_tool_call("delegate_to_agent", '{"action":"%s"}' % action)


def test_delegation_run_and_start_remain_capacity_limited():
    assert _capacity_limited_tool_call("delegate_to_agent", '{"action":"run"}')
    assert _capacity_limited_tool_call("delegate_to_claude_code", '{"action":"start"}')


def test_capacity_result_is_actionable_and_machine_readable():
    result = _worker_capacity_result("delegate_to_agent", 1, 1)
    assert result["exit_code"] == 1
    assert result["blocked_reason"] == "worker_capacity"
    assert result["capacity"] == {"limit": 1, "active": 1, "available": 0}
    assert "poll" in result["error"]
    assert result["capacity_scope"] == "parent_chat"
    assert "provider-wide" in result["error"]


def test_parent_worker_limit_is_independent_of_provider_capacity(monkeypatch):
    from src.session_settings import effective_worker_limit
    from src.agent_tools import claude_code_tools

    monkeypatch.setattr(claude_code_tools, "_setting", lambda key, default=None: 8)
    assert claude_code_tools.configured_concurrency() == 8
    assert effective_worker_limit({}) == 1
    assert effective_worker_limit({"max_parallel_workers": 8}) == 8
    assert effective_worker_limit({"max_parallel_workers": 0}) == 0
    assert effective_worker_limit({"max_parallel_workers": None}) == 1
    assert effective_worker_limit({"max_parallel_workers": "bad"}) == 1


def test_normalized_delegation_state_precedes_conflicting_raw_provider_output():
    from src.tool_execution import format_tool_result

    rendered = format_tool_result("delegate_to_agent", {
        "output": "Started three audits",
        "exit_code": 1,
        "delegation_state": "failed",
        "delegation_note": "No worker is confirmed.",
    })
    assert rendered.index("**delegation:** `failed`") < rendered.index("Started three audits")


def test_app_api_schema_advertises_bounded_endpoint_paging():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    schema = next(item["function"] for item in FUNCTION_TOOL_SCHEMAS if item["function"]["name"] == "app_api")
    props = schema["parameters"]["properties"]
    assert props["limit"]["minimum"] == 1 and props["limit"]["maximum"] == 50
    assert props["offset"]["minimum"] == 0


async def test_empty_session_policy_still_blocks_start_at_queued_claude_capacity(monkeypatch):
    """The CLI runner records a queued task before an activity run exists."""
    from core import database
    from src import agent_activity
    from src.agent_tools import claude_code_tools
    from src.tool_execution import execute_tool_block
    from src.tool_types import ToolBlock

    class Runner:
        def summaries(self, *, limit):
            return [{"task_id": "queued-1", "session_id": "chat-1", "status": "queued"}]

    monkeypatch.setattr(database, "get_session_settings", lambda sid: {})
    monkeypatch.setattr(agent_activity, "list_runs", lambda *, limit: [])
    monkeypatch.setattr(claude_code_tools, "get_task_runner", lambda: Runner())
    desc, result = await execute_tool_block(
        ToolBlock("delegate_to_agent", '{"action":"start","prompt":"audit"}'), session_id="chat-1"
    )

    assert desc.endswith("BLOCKED")
    assert result["blocked_reason"] == "worker_capacity"
    assert result["capacity"] == {"limit": 1, "active": 1, "available": 0}


async def test_poll_is_executed_while_empty_policy_is_at_capacity(monkeypatch):
    from core import database
    from src import agent_control
    import src.tool_execution as execution
    from src.tool_execution import execute_tool_block
    from src.tool_types import ToolBlock

    async def fake_fallback(*args, **kwargs):
        return {"output": "poll result", "exit_code": 0}

    monkeypatch.setattr(database, "get_session_settings", lambda sid: {})
    monkeypatch.setattr(agent_control, "live_children", lambda sid: 1)
    monkeypatch.setattr(execution, "_owner_is_admin", lambda owner: True)
    monkeypatch.setattr(execution, "_direct_fallback", fake_fallback)
    desc, result = await execute_tool_block(
        ToolBlock("delegate_to_agent", '{"action":"poll","task_id":"queued-1"}'), session_id="chat-1"
    )

    assert "BLOCKED" not in desc
    assert result == {"output": "poll result", "exit_code": 0}
