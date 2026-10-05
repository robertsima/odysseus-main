"""Task- and chat-scoped approval continuation coverage for issue #6112."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

from core.models import ChatMessage, Session
from src.tool_approval_scopes import (
    CHAT_SESSION_APPROVAL_CONTEXT_MARKER,
    ToolApprovalScope,
    stamp_chat_session_grant,
)
from src.tool_approvals import ExactToolApproval, ToolApprovalStore
from src.tool_capabilities import ToolRunSecurityContext, capabilities_for_action


def _pending(
    store: ToolApprovalStore,
    *,
    selected_tools=None,
    continuation_query="inspect the project using memory and skills",
):
    content = "printf exact"
    return store.create(
        owner="Alice",
        session_id="session-1",
        origin_run_id="run-1",
        tool_name="bash",
        content=content,
        workspace=None,
        external_untrusted_context_seen=True,
        selected_tools=selected_tools,
        continuation_query=continuation_query,
        capabilities=capabilities_for_action("bash", content),
    )


def test_card_offers_task_chat_session_and_deny_without_leaking_private_state():
    pending = _pending(
        ToolApprovalStore(),
        selected_tools=["manage_skills", "bash", "manage_skills"],
    )

    payload = pending.public_payload()

    assert payload["session_id"] == "session-1"
    assert [option["value"] for option in payload["options"]] == [
        "approve_task",
        "approve",
        "deny",
    ]
    assert [option["label"] for option in payload["options"]] == [
        "Allow for this task",
        "Allow for this chat session",
        "Deny",
    ]
    serialized = json.dumps(payload, sort_keys=True)
    assert "Allow once" not in serialized
    assert "selected_tools" not in serialized
    assert "continuation_query" not in serialized
    assert "manage_skills" not in serialized
    assert "inspect the project" not in serialized


def test_allow_for_task_bypasses_only_the_resumed_run_gate():
    store = ToolApprovalStore()
    pending = _pending(store, selected_tools=["bash", "manage_skills"])
    grant = store.consume(
        pending.approval_id,
        decision="approve_task",
        owner="alice",
        session_id="session-1",
    )

    assert grant is not None
    assert grant.scope is ToolApprovalScope.TASK
    assert grant.allow_remaining_actions is True
    assert grant.grants_chat_session is False
    assert grant.pending.continuation_query == (
        "inspect the project using memory and skills"
    )

    resumed = ToolRunSecurityContext(
        external_untrusted_context_seen=True,
        approval_gate_bypassed=grant.allow_remaining_actions,
    )
    assert resumed.decision_for("bash").allowed is True

    # A new ordinary user turn constructs a fresh context and asks again.
    fresh = ToolRunSecurityContext(external_untrusted_context_seen=True)
    assert fresh.decision_for("bash").allowed is False


def test_allow_for_chat_session_applies_to_later_turns_in_only_that_chat():
    store = ToolApprovalStore()
    pending = _pending(store, selected_tools=["bash", "manage_skills"])
    grant = store.consume(
        pending.approval_id,
        decision="approve",
        owner="alice",
        session_id="session-1",
    )

    assert grant is not None
    assert grant.scope is ToolApprovalScope.CHAT_SESSION
    assert grant.allow_remaining_actions is True
    assert grant.grants_chat_session is True
    assert grant.pending.selected_tools == ("bash", "manage_skills")
    assert grant.pending.continuation_query.startswith("inspect the project")

    resolved_card = pending.public_payload()
    resolved_card["resolved"] = "approve"
    # Resolving is a server action, and only the server's signature on the card
    # makes it a grant. A card that merely looks resolved is not one.
    stamp_chat_session_grant(resolved_card, "session-1", "approve")
    history = [
        ChatMessage(
            "assistant",
            "approval requested",
            {"tool_events": [{"ask_user": resolved_card}]},
        ),
        ChatMessage("user", "continue the work"),
    ]
    session = Session(
        id="session-1",
        name="Chat",
        endpoint_url="http://example.invalid",
        model="test",
        history=history,
    )

    messages = session.get_context_messages()
    assert messages[-1]["metadata"][CHAT_SESSION_APPROVAL_CONTEXT_MARKER] is True
    assert history[-1].metadata is None

    future_turn = ToolRunSecurityContext(external_untrusted_context_seen=True)
    future_turn.observe_messages(messages)
    assert future_turn.approval_gate_bypassed is True
    assert future_turn.decision_for("bash").allowed is True

    # The persisted card is bound to its original chat id, so a fork/copy does
    # not inherit the grant merely by copying transcript metadata.
    other_session = Session(
        id="session-2",
        name="Fork",
        endpoint_url="http://example.invalid",
        model="test",
        history=history,
    )
    other_messages = other_session.get_context_messages()
    assert CHAT_SESSION_APPROVAL_CONTEXT_MARKER not in (
        other_messages[-1].get("metadata") or {}
    )
    other_turn = ToolRunSecurityContext(external_untrusted_context_seen=True)
    other_turn.observe_messages(other_messages)
    assert other_turn.decision_for("bash").allowed is False


def test_deny_executes_nothing_and_grants_no_task_or_chat_scope():
    store = ToolApprovalStore()
    pending = _pending(store)

    assert store.consume(
        pending.approval_id,
        decision="deny",
        owner="alice",
        session_id="session-1",
    ) is None
    assert store.peek(pending.approval_id) is None

    denied_card = pending.public_payload()
    denied_card["resolved"] = "deny"
    session = Session(
        id="session-1",
        name="Chat",
        endpoint_url="http://example.invalid",
        model="test",
        history=[
            ChatMessage(
                "assistant",
                "approval requested",
                {"tool_events": [{"ask_user": denied_card}]},
            ),
            ChatMessage("user", "another request"),
        ],
    )
    messages = session.get_context_messages()
    assert CHAT_SESSION_APPROVAL_CONTEXT_MARKER not in (
        messages[-1].get("metadata") or {}
    )


def test_private_continuation_state_is_canonical_bounded_and_digest_bound():
    selected_tools = ["manage_skills", "bash", "manage_skills", "", 7]
    selected_tools.extend(f"tool_{index:04d}" for index in range(600))
    selected_tools.append("x" * 513)
    pending = _pending(
        ToolApprovalStore(),
        selected_tools=selected_tools,
        continuation_query="  " + ("original request " * 500),
    )
    assert pending.selected_tools[:2] == ("bash", "manage_skills")
    assert len(pending.selected_tools) == 512
    assert all(len(name) <= 512 for name in pending.selected_tools)
    assert "x" * 513 not in pending.selected_tools
    assert pending.continuation_query.startswith("original request")
    assert len(pending.continuation_query) == 4000

    tampered = replace(
        pending,
        selected_tools=("bash", "manage_skills", "send_email"),
        continuation_query="different request",
    )
    grant = ExactToolApproval(tampered)
    assert grant.matches(
        owner="alice",
        session_id="session-1",
        tool_name="bash",
        content="printf exact",
        workspace=None,
    ) is False


def test_consumed_card_resolution_updates_memory_and_persisted_metadata(monkeypatch):
    from routes import chat_routes

    ask_user = {
        "kind": "tool_approval",
        "approval_id": "approval-1",
        "session_id": "session-1",
    }
    metadata = {
        "_db_id": "message-1",
        "tool_events": [{"ask_user": ask_user}],
    }
    sess = SimpleNamespace(
        id="session-1",
        history=[SimpleNamespace(metadata=metadata)],
    )
    db_message = SimpleNamespace(meta_data=None)

    class Column:
        def __eq__(self, value):
            return value

    class FakeDBMessage:
        id = Column()
        session_id = Column()

    class FakeQuery:
        def filter(self, *conditions):
            return self

        def first(self):
            return db_message

    class FakeDB:
        committed = False
        rolled_back = False
        closed = False

        def query(self, model):
            assert model is FakeDBMessage
            return FakeQuery()

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    db = FakeDB()
    monkeypatch.setattr(chat_routes, "DBChatMessage", FakeDBMessage)
    monkeypatch.setattr(chat_routes, "SessionLocal", lambda: db)

    assert chat_routes._mark_tool_approval_resolved(
        sess,
        "approval-1",
        "approve",
    ) is True
    assert ask_user["resolved"] == "approve"
    persisted = json.loads(db_message.meta_data)
    assert persisted["tool_events"][0]["ask_user"]["resolved"] == "approve"
    assert "_db_id" not in persisted
    assert db.committed is True
    assert db.rolled_back is False
    assert db.closed is True


def test_deny_resolution_stream_is_control_only():
    from routes.chat_routes import _tool_approval_resolution_stream

    async def collect():
        return [chunk async for chunk in _tool_approval_resolution_stream("deny")]

    chunks = asyncio.run(collect())
    assert chunks[-1] == "data: [DONE]\n\n"
    event = json.loads(chunks[0][len("data: "):])
    assert event == {"type": "tool_approval_resolved", "decision": "deny"}
    assert "Denied the" not in "".join(chunks)


