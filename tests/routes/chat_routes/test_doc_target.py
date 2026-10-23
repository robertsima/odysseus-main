"""The open document reaches the model only when the chat's target is "document".

Bug (2026-10-23): opening a document made every message about it. The route looked
the document up four ways (explicit id, an email draft of the chat, any active
document of the chat, the tool layer's in-memory pointer), so even a person who
chose "General conversation" had it injected and its tools switched on.
"""
import pytest

import src.database
from core.database import Document
from src.agent_tools import document_tools
from tests.routes.chat_routes.agent_turn import agent_turn  # noqa: F401  (fixture)


@pytest.fixture
def docs(agent_turn):
    db = src.database.SessionLocal()
    try:
        db.add(Document(id="open-doc", title="DA-02", current_content="body", owner="admin",
                        session_id=agent_turn.session_id, is_active=True, language="markdown"))
        db.add(Document(id="draft", title="Reply", current_content="body", owner="admin",
                        session_id=agent_turn.session_id, is_active=True, language="email"))
        db.commit()
    finally:
        db.close()
    return agent_turn


def _doc(turn):
    doc = turn.loop_kwargs.get("active_document")
    return doc.id if doc else None


def test_the_document_target_injects_the_explicit_document(docs):
    turn = docs.send({"message": "summarize the open document", "mode": "agent", "active_doc_id": "open-doc", "doc_target": "document"})
    assert _doc(turn) == "open-doc"


def test_an_explicit_id_with_no_target_still_injects(docs):
    assert _doc(docs.send({"message": "summarize the open document", "mode": "agent", "active_doc_id": "open-doc"})) == "open-doc"


@pytest.mark.parametrize("target", ["general", "project"])
def test_other_targets_inject_nothing_even_with_an_explicit_id(docs, target):
    turn = docs.send({"message": "summarize the open document", "mode": "agent", "active_doc_id": "open-doc", "doc_target": target})
    assert _doc(turn) is None


@pytest.mark.parametrize("target", ["general", "project"])
def test_other_targets_ignore_the_session_bound_fallbacks(docs, target):
    turn = docs.send({"message": "summarize the open document", "mode": "agent", "doc_target": target})
    assert _doc(turn) is None


def test_without_a_target_the_session_fallback_still_finds_the_document(docs):
    assert _doc(docs.send({"message": "summarize the open document", "mode": "agent"})) is not None


def test_the_in_memory_pointer_is_ignored_for_general(docs):
    db = src.database.SessionLocal()
    try:
        db.query(Document).filter(Document.session_id == docs.session_id).update({"session_id": None})
        db.commit()
    finally:
        db.close()
    document_tools.set_active_document("open-doc", docs.session_id)
    try:
        assert _doc(docs.send({"message": "summarize the open document", "mode": "agent"})) == "open-doc"  # the pointer works for document
        document_tools.set_active_document("open-doc", docs.session_id)
        assert _doc(docs.send({"message": "summarize the open document", "mode": "agent", "doc_target": "general"})) is None
    finally:
        document_tools.clear_active_document(session_id=docs.session_id)


def test_a_general_turn_does_not_rebind_a_document_to_this_chat(docs):
    db = src.database.SessionLocal()
    try:
        db.add(Document(id="elsewhere", title="Other", current_content="b", owner="admin",
                        session_id=None, is_active=True, language="markdown"))
        db.commit()
    finally:
        db.close()

    docs.send({"message": "summarize the open document", "mode": "agent", "active_doc_id": "elsewhere", "doc_target": "general"})

    db = src.database.SessionLocal()
    try:
        assert db.query(Document).filter_by(id="elsewhere").one().session_id is None
    finally:
        db.close()


def test_the_saved_target_applies_when_the_turn_sends_none(docs):
    docs.client.patch(f"/api/session/{docs.session_id}/settings", json={"doc_target": "general"})
    assert _doc(docs.send({"message": "summarize the open document", "mode": "agent", "active_doc_id": "open-doc"})) is None


def test_the_turns_target_beats_the_saved_one(docs):
    docs.client.patch(f"/api/session/{docs.session_id}/settings", json={"doc_target": "general"})
    turn = docs.send({"message": "summarize the open document", "mode": "agent", "active_doc_id": "open-doc", "doc_target": "document"})
    assert _doc(turn) == "open-doc"


def test_an_approval_continuation_still_gets_its_document(docs):
    """The sealed approval record names the document; a general target must not strip it."""
    from src.tool_approvals import tool_approval_store, document_content_digest
    from src.tool_capabilities import capabilities_for_action

    content = '{"content":"replacement"}'
    pending = tool_approval_store.create(
        owner="admin", session_id=docs.session_id, origin_run_id="run-1", tool_name="update_document",
        content=content, workspace=None, document_id="open-doc", document_version=1,
        document_digest=document_content_digest("body"), external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("update_document", content),
    )

    turn = docs.send({"message": "", "mode": "agent", "doc_target": "general",
                      "tool_approval_id": pending.approval_id, "tool_approval_decision": "approve"})

    assert _doc(turn) == "open-doc"
