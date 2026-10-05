"""With an email open in the reader, the turn cannot send mail or invent a draft.

The only compose path left is ui_control open_email_reply, which opens the
reader's own draft editor. Without the guard a model that botched the draft
call fell back to SMTP (send_email / reply_to_email), sending unreviewed mail,
or wrote an email-shaped document instead.
"""
import pytest

from tests.routes.chat_routes.agent_turn import agent_turn  # noqa: F401  (fixture)

pytestmark = pytest.mark.security

DIRECT_COMPOSE_TOOLS = {
    "send_email", "reply_to_email", "create_document",
    "mcp__email__send_email", "mcp__email__reply_to_email",
}


def _disabled(turn) -> set:
    return set(turn.loop_kwargs.get("disabled_tools") or ())


def test_an_open_email_withholds_the_direct_send_and_draft_tools(agent_turn):
    turn = agent_turn.send({
        "message": "write a short reply saying thanks", "mode": "agent",
        "active_email_uid": "42", "active_email_folder": "INBOX",
    })

    assert DIRECT_COMPOSE_TOOLS <= _disabled(turn)
    assert turn.loop_kwargs["active_email"]["uid"] == "42"


def test_without_an_open_email_those_tools_stay_available(agent_turn):
    turn = agent_turn.send({"message": "write a short reply saying thanks", "mode": "agent"})

    assert not DIRECT_COMPOSE_TOOLS & _disabled(turn)
