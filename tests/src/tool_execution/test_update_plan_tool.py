"""`update_plan` — the agent keeps the chat's task checklist (tick done / revise).

`execute_tool_block` returns a `plan_update` payload the agent loop turns into a
`plan_update` SSE event, and the checklist is saved on the chat
(src.task_checklist) so later turns are shown it. Does not end the turn.
"""
import asyncio
import json

from src.agent_tools import ToolBlock, TOOL_TAGS  # import first to avoid circular
from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block
from src.tool_index import ALWAYS_AVAILABLE, BUILTIN_TOOL_DESCRIPTIONS
from src.tool_security import is_public_blocked_tool


def _run(content):
    return asyncio.run(execute_tool_block(
        ToolBlock("update_plan", content),
        security_context=NO_TOOL_SECURITY_CONTEXT,
    ))


def test_valid_plan_returns_marker_and_counts():
    plan = "- [x] step one\n- [ ] step two\n- [ ] step three"
    desc, result = _run(json.dumps({"plan": plan}))
    assert result.get("exit_code") == 0
    assert result["plan_update"]["plan"] == plan
    assert "1/3" in result["output"]   # 1 done of 3


def test_plain_string_accepted():
    plan = "- [ ] a\n- [x] b"
    _, result = _run(plan)
    assert result["plan_update"]["plan"] == plan


def test_empty_plan_clears_the_checklist():
    _, result = _run(json.dumps({"plan": "   "}))
    assert result.get("exit_code") == 0
    assert result["plan_update"] == {"plan": ""}
    assert "cleared" in result["output"].lower()


def test_registered_everywhere():
    assert "update_plan" in TOOL_TAGS
    assert "update_plan" in ALWAYS_AVAILABLE
    assert "update_plan" in BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    assert "update_plan" in {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    # Not admin/public-gated — any user can drive their own plan.
    assert is_public_blocked_tool("update_plan") is False
