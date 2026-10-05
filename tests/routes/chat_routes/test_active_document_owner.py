"""A chat only ever puts its own user's documents in front of the model.

/api/chat_stream injects the open document: the id the editor sends, else
the newest document bound to the chat. Bob's document must not reach
alice's prompt by either route, and naming it must not rebind it to her chat.
"""
import json

import httpx
import pytest

import src.database
import src.llm_core
from core.database import Document

pytestmark = pytest.mark.security

ALICE_TEXT = "ALICE GROCERY LIST"
BOB_TEXT = "BOB SECRET MERGER PLAN"


@pytest.fixture
def chat(api, monkeypatch):
    db = src.database.SessionLocal()
    try:
        db.add(src.database.ModelEndpoint(
            id="alice-ep", name="alice endpoint", base_url="http://alice-ep.test/v1", is_enabled=True,
            owner="alice", cached_models=json.dumps(["alice-model"]),
        ))
        db.commit()
    finally:
        db.close()
    alice = api.as_user("alice")
    created = alice.post("/api/session", data={
        "name": "planning", "endpoint_id": "alice-ep", "model": "alice-model", "skip_validation": "true",
    })
    assert created.status_code == 200, created.text
    session_id = created.json()["id"]
    sent = []

    async def send(self, request, **kwargs):
        sent.append(request.content.decode("utf-8", "replace") if request.content else "")
        raise httpx.ConnectError("no network in tests", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    # Failed calls cool the host for 20 s; start each test with none cooled.
    monkeypatch.setattr(src.llm_core, "_dead_hosts", {})
    monkeypatch.setattr(src.llm_core, "_host_fails", {})
    bob_chat = api.as_user("bob").post("/api/session", data={"name": "bob's", "skip_validation": "true"})
    assert bob_chat.status_code == 200, bob_chat.text

    def add_document(doc_id, owner, text, doc_session=None):
        db = src.database.SessionLocal()
        try:
            db.add(Document(id=doc_id, title=doc_id, current_content=text, owner=owner,
                            session_id=doc_session, is_active=True, language="markdown"))
            db.commit()
        finally:
            db.close()

    def send_message(**form):
        sent.clear()
        alice.post("/api/chat_stream", data={"message": "summarize the open document", "session": session_id, **form})
        return "\n".join(sent)

    def session_of(doc_id):
        db = src.database.SessionLocal()
        try:
            return db.query(Document).filter(Document.id == doc_id).one().session_id
        finally:
            db.close()

    return {"session": session_id, "bob_session": bob_chat.json()["id"], "add": add_document,
            "send": send_message, "session_of": session_of}


def test_the_open_document_reaches_the_model(chat):
    chat["add"]("doc-alice", "alice", ALICE_TEXT)

    prompt = chat["send"](active_doc_id="doc-alice")

    assert ALICE_TEXT in prompt


def test_naming_another_users_document_does_not_inject_or_rebind_it(chat):
    chat["add"]("doc-bob", "bob", BOB_TEXT, doc_session=chat["bob_session"])

    prompt = chat["send"](active_doc_id="doc-bob")

    assert prompt, "the turn never reached the model"
    assert BOB_TEXT not in prompt
    assert chat["session_of"]("doc-bob") == chat["bob_session"]


def test_another_users_document_bound_to_this_chat_is_not_injected(chat):
    chat["add"]("doc-bob", "bob", BOB_TEXT, doc_session=chat["session"])

    prompt = chat["send"]()

    assert prompt, "the turn never reached the model"
    assert BOB_TEXT not in prompt
