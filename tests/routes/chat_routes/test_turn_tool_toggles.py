"""A turn's shell and web settings decide which tools the agent loop gets.

POST /api/chat_stream turns the composer's toggles (or an API caller's JSON
fields) into the disabled-tools set handed to the agent loop. The shell
follows the account's privilege unless the turn switches it off; web tools
need the turn to switch them on. Auto-escalation from chat mode keeps the
shell for workspace work and the browser for a follow-up to a browser task.
"""
import pytest

from routes.chat_routes import _BROWSER_MCP_TOOLS
from src.tool_policy import WEB_TOOL_NAMES
from tests.routes.chat_routes.agent_turn import DONE, agent_turn, frame  # noqa: F401  (fixture)

pytestmark = pytest.mark.security

SHELL_TOOLS = {"bash", "python", "read_file", "write_file"}


def disabled(turn) -> set:
    return set(turn.loop_kwargs.get("disabled_tools") or ())


def test_an_api_callers_json_fields_set_the_shell_and_web_tools(agent_turn):
    """API callers post JSON, not a form (#3229)."""
    turn = agent_turn.send(
        {"message": "hello", "allow_bash": "false", "allow_web_search": "true"}, as_json=True)

    assert "bash" in disabled(turn)
    assert not set(WEB_TOOL_NAMES) & disabled(turn)


def test_the_shell_follows_the_account_unless_the_turn_switches_it_off(agent_turn):
    unset = agent_turn.send({"message": "hello", "mode": "agent"})
    switched_off = agent_turn.send({"message": "hello", "mode": "agent", "allow_bash": "false"})

    assert "bash" not in disabled(unset)
    assert "bash" in disabled(switched_off)


def test_web_tools_need_the_turn_to_switch_them_on(agent_turn):
    unset = agent_turn.send({"message": "hello", "mode": "agent"})
    switched_on = agent_turn.send({"message": "hello", "mode": "agent", "allow_web_search": "true"})

    assert set(WEB_TOOL_NAMES) <= disabled(unset)
    assert not set(WEB_TOOL_NAMES) & disabled(switched_on)


def test_a_chat_turn_asking_for_workspace_work_keeps_the_shell(agent_turn):
    turn = agent_turn.send({"message": "run the test suite in my repo and fix the failing test", "mode": "chat"})

    assert not SHELL_TOOLS & disabled(turn)


def test_a_chat_turn_escalated_for_notes_does_not_get_the_shell(agent_turn):
    turn = agent_turn.send({"message": "remind me to call the dentist tomorrow at 9", "mode": "chat"})

    assert SHELL_TOOLS <= disabled(turn)


def test_a_short_approval_after_a_form_turn_is_a_browser_turn(agent_turn):
    agent_turn.script = [frame({"delta": "The contact form on example.com is filled in. Submit it?"}),
                         frame({"type": "metrics", "data": {}}), DONE]
    agent_turn.send({"message": "fill out the contact form on example.com", "mode": "agent"})
    agent_turn.script = [frame({"delta": "Submitted."}), frame({"type": "metrics", "data": {}}), DONE]

    turn = agent_turn.send({"message": "approved", "mode": "chat"})

    assert set(_BROWSER_MCP_TOOLS) <= set(turn.loop_kwargs.get("forced_tools") or ())
