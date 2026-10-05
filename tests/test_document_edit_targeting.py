"""Document edit tools act on exactly the document they name.

Production incident (2026-09-28): the admin chat was asked to update an
existing note. ``edit_document`` had no document-id argument, the in-memory
"active document" was one process-wide id (empty after a restart), and the
tools silently fell back to the most recently updated document. Three edits
landed on a different document that shared the note's title, while the reply
linked the intended one as "updated".

These tests pin the fix: an explicit ``document_id`` (bare id or link form)
targets exactly that document, the active document is per chat session, and
without a target the tools refuse with a candidate list instead of guessing.
"""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

from core.database import Document, DocumentVersion  # noqa: E402

import src.agent_tools.document_tools as dt  # noqa: E402
from src.agent_tools import TOOL_HANDLERS  # noqa: E402

TITLE = "Agent Memory and RAG Business Opportunity Research"


@pytest.fixture
def db(monkeypatch, app_db):
    import core.database as core_database
    import src.database as legacy_database

    SessionLocal = app_db.SessionLocal
    monkeypatch.setattr(core_database, "SessionLocal", SessionLocal)
    monkeypatch.setattr(legacy_database, "SessionLocal", SessionLocal)
    monkeypatch.setattr(dt, "_missing_document_upload", lambda owner, content: None)
    dt.clear_active_document()
    yield SessionLocal
    dt.clear_active_document()


def _add_doc(SessionLocal, *, title=TITLE, content="Status: draft\nBody", owner="alice",
             session_id=None, age_minutes=0, doc_id=None):
    doc_id = doc_id or str(uuid.uuid4())
    s = SessionLocal()
    try:
        ts = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=age_minutes)
        s.add(Document(
            id=doc_id, session_id=session_id, title=title, language="markdown",
            current_content=content, version_count=1, is_active=True, owner=owner,
            created_at=ts, updated_at=ts,
        ))
        s.add(DocumentVersion(
            id=str(uuid.uuid4()), document_id=doc_id, version_number=1,
            content=content, summary="seed", source="user",
        ))
        s.commit()
    finally:
        s.close()
    return doc_id


def _doc(SessionLocal, doc_id):
    s = SessionLocal()
    try:
        d = s.query(Document).filter(Document.id == doc_id).first()
        return (d.current_content, d.version_count, d.is_active)
    finally:
        s.close()


EDIT = "<<<FIND>>>\nStatus: draft\n<<<REPLACE>>>\nStatus: final\n<<<END>>>"


def _run(tool, content, **ctx):
    ctx.setdefault("owner", "alice")
    return asyncio.run(TOOL_HANDLERS[tool](content, ctx))


def _native(name, args):
    from src.tool_schemas import function_call_to_tool_block

    block = function_call_to_tool_block(name, json.dumps(args))
    assert block is not None
    return block.content


# ---------------------------------------------------------------------------
# Explicit targets
# ---------------------------------------------------------------------------

def test_explicit_id_targets_that_doc_among_same_titled_docs(db):
    intended = _add_doc(db, age_minutes=30)       # the note the user meant
    decoy = _add_doc(db, age_minutes=0)           # same title, updated last
    dt.set_active_document(decoy, "chat-1")       # even a different active doc

    content = _native("edit_document", {
        "document_id": intended,
        "edits": [{"find": "Status: draft", "replace": "Status: final"}],
    })
    result = _run("edit_document", content, session_id="chat-1")

    assert result.get("error") is None, result
    assert result["doc_id"] == intended
    assert result["title"] == TITLE
    assert f"#document-{intended}" in result["document_summary"]
    assert result["document_summary"].startswith(f'Edited document "{TITLE}"')
    assert _doc(db, intended)[:2] == ("Status: final\nBody", 2)
    assert _doc(db, decoy)[:2] == ("Status: draft\nBody", 1)
    # The explicitly edited doc becomes this chat's active document.
    assert dt.get_active_document("chat-1") == intended


@pytest.mark.parametrize("form", [
    "#document-{id}",
    "document-{id}",
    "[Some title](#document-{id})",
    "  {id}  ",
])
def test_link_form_ids_resolve_to_the_same_doc(db, form):
    intended = _add_doc(db, age_minutes=30)
    _add_doc(db, age_minutes=0)

    content = _native("edit_document", {
        "document_id": form.format(id=intended),
        "edits": [{"find": "Status: draft", "replace": "Status: final"}],
    })
    result = _run("edit_document", content, session_id="chat-1")

    assert result.get("doc_id") == intended, result
    assert _doc(db, intended)[0] == "Status: final\nBody"


def test_update_document_explicit_id_and_header_not_written(db):
    intended = _add_doc(db, age_minutes=30)
    decoy = _add_doc(db, age_minutes=0)

    content = _native("update_document", {
        "document_id": f"#document-{intended}",
        "content": "Rewritten body",
    })
    assert content.startswith("<<<DOCUMENT_ID:")
    result = _run("update_document", content, session_id="chat-1")

    assert result["doc_id"] == intended
    assert result["action"] == "update"
    assert result["document_summary"].startswith(f'Updated document "{TITLE}"')
    assert _doc(db, intended)[0] == "Rewritten body"   # header stripped
    assert _doc(db, decoy)[1] == 1


def test_suggest_document_explicit_id(db):
    intended = _add_doc(db, age_minutes=30)
    _add_doc(db, age_minutes=0)

    content = _native("suggest_document", {
        "document_id": intended,
        "suggestions": [{"find": "Status: draft", "replace": "Status: final", "reason": "done"}],
    })
    result = _run("suggest_document", content, session_id="chat-1")

    assert result["doc_id"] == intended
    assert result["count"] == 1
    assert _doc(db, intended)[1] == 1  # suggestions never write


def test_title_reference_shared_by_several_docs_is_refused(db):
    a = _add_doc(db, age_minutes=30)
    b = _add_doc(db, age_minutes=0)

    result = _run("edit_document", dt.with_document_id_header(EDIT, TITLE), session_id="chat-1")

    assert result["exit_code"] == 1
    assert result.get("ambiguous") is True
    assert {c["id"] for c in result["document_candidates"]} == {a, b}
    assert a in result["error"] and b in result["error"]
    assert _doc(db, a)[1] == 1 and _doc(db, b)[1] == 1


def test_unique_title_or_id_prefix_resolves(db):
    only = _add_doc(db, title="Unique note")
    _add_doc(db, title="Something else")

    by_title = _run("edit_document", dt.with_document_id_header(EDIT, "unique NOTE"), session_id="c")
    assert by_title["doc_id"] == only

    other = _add_doc(db, title="Prefix note", doc_id="a86f5585-1111-4111-8111-111111111111")
    by_prefix = _run("edit_document", dt.with_document_id_header(EDIT, "a86f5585"), session_id="c")
    assert by_prefix["doc_id"] == other


def test_explicit_id_of_another_owners_doc_is_not_found(db):
    bobs = _add_doc(db, owner="bob")
    mine = _add_doc(db, title="Mine")

    result = _run("edit_document", dt.with_document_id_header(EDIT, bobs), session_id="chat-1")

    assert result["exit_code"] == 1
    assert "not found" in result["error"]
    assert [c["id"] for c in result["document_candidates"]] == [mine]
    assert _doc(db, bobs)[1] == 1


# ---------------------------------------------------------------------------
# No target → refuse with candidates, never "most recent"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool,content", [
    ("edit_document", EDIT),
    ("update_document", "Replacement"),
    ("suggest_document", "<<<FIND>>>\nStatus: draft\n<<<SUGGEST>>>\nx\n<<<REASON>>>\ny\n<<<END>>>"),
])
def test_no_id_and_no_active_doc_errors_with_candidates(db, tool, content):
    older = _add_doc(db, age_minutes=30)
    newest = _add_doc(db, age_minutes=0)
    _add_doc(db, owner="bob", title="Bob private")

    result = _run(tool, content, session_id="fresh-chat")

    assert result["exit_code"] == 1
    assert result["needs_document_id"] is True
    ids = [c["id"] for c in result["document_candidates"]]
    assert ids == [newest, older]                 # owner's docs only, newest first
    assert all(c["updated_at"] and c["title"] == TITLE for c in result["document_candidates"])
    assert "Bob private" not in result["error"]
    assert "document_id" in result["error"]
    assert _doc(db, older)[:2] == ("Status: draft\nBody", 1)
    assert _doc(db, newest)[:2] == ("Status: draft\nBody", 1)
    assert dt.get_active_document("fresh-chat") is None


def test_candidate_list_is_limited(db):
    for i in range(dt._CANDIDATE_LIMIT + 5):
        _add_doc(db, title=f"Doc {i}", age_minutes=i)

    result = _run("edit_document", EDIT, session_id="fresh-chat")

    assert len(result["document_candidates"]) == dt._CANDIDATE_LIMIT


def test_stale_active_pointer_is_refused_not_replaced(db):
    gone = _add_doc(db, title="Deleted")
    other = _add_doc(db)
    s = db()
    s.query(Document).filter(Document.id == gone).update({"is_active": False})
    s.commit()
    s.close()
    dt.set_active_document(gone, "chat-1")

    result = _run("edit_document", EDIT, session_id="chat-1")

    assert result["exit_code"] == 1
    assert gone in result["error"]
    assert _doc(db, other)[1] == 1


def test_manage_documents_delete_without_target_deletes_nothing(db):
    a = _add_doc(db, age_minutes=0)

    result = asyncio.run(TOOL_HANDLERS["manage_documents"](
        '{"action":"delete"}', {"owner": "alice", "session_id": "fresh-chat"},
    ))

    assert result["exit_code"] == 1
    assert _doc(db, a)[2] is True

    dt.set_active_document(a, "chat-1")
    ok = asyncio.run(TOOL_HANDLERS["manage_documents"](
        json.dumps({"action": "delete", "document_id": f"#document-{a}"}),
        {"owner": "alice", "session_id": "chat-2"},
    ))
    assert ok["exit_code"] == 0
    assert _doc(db, a)[2] is False
    assert dt.get_active_document("chat-1") is None


# ---------------------------------------------------------------------------
# Per-session active document
# ---------------------------------------------------------------------------

def test_active_document_is_isolated_per_chat_session(db):
    doc_1 = _add_doc(db, title="Chat one doc")
    doc_2 = _add_doc(db, title="Chat two doc")
    dt.set_active_document(doc_1, "chat-1")
    dt.set_active_document(doc_2, "chat-2")

    r1 = _run("edit_document", EDIT, session_id="chat-1")
    r2 = _run("update_document", "Chat two rewrite", session_id="chat-2")
    r3 = _run("edit_document", EDIT, session_id="chat-3")

    assert r1["doc_id"] == doc_1
    assert r2["doc_id"] == doc_2
    assert r3["exit_code"] == 1 and r3["needs_document_id"] is True
    assert _doc(db, doc_1)[0] == "Status: final\nBody"
    assert _doc(db, doc_2)[0] == "Chat two rewrite"
    assert dt.get_active_document("chat-1") == doc_1
    assert dt.get_active_document("chat-2") == doc_2


def test_followup_edit_without_id_stays_on_the_explicit_doc(db):
    intended = _add_doc(db, content="Status: draft\nOwner: me", age_minutes=30)
    decoy = _add_doc(db, age_minutes=0)

    first = _run("edit_document", dt.with_document_id_header(EDIT, intended), session_id="chat-1")
    second = _run(
        "edit_document",
        "<<<FIND>>>\nOwner: me\n<<<REPLACE>>>\nOwner: you\n<<<END>>>",
        session_id="chat-1",
    )

    assert first["doc_id"] == intended == second["doc_id"]
    assert _doc(db, intended)[:2] == ("Status: final\nOwner: you", 3)
    assert _doc(db, decoy)[1] == 1


def test_clear_active_document_by_doc_clears_every_session():
    dt.clear_active_document()
    dt.set_active_document("doc-x", "a")
    dt.set_active_document("doc-x", "b")
    dt.set_active_document("doc-y", "c")

    assert dt.clear_active_document("doc-x") is True
    assert dt.get_active_document("a") is None
    assert dt.get_active_document("b") is None
    assert dt.get_active_document("c") == "doc-y"
    assert dt.clear_active_document("doc-y", session_id="a") is False
    assert dt.clear_active_document(session_id="c") is True
    assert dt.get_active_document("c") is None


def test_create_document_becomes_that_sessions_active_doc(db):
    from core.database import Session as DbSession

    s = db()
    s.add(DbSession(id="chat-9", name="n", endpoint_url="http://x", model="m", owner="alice"))
    s.commit()
    s.close()

    created = _run("create_document", "New note\nmarkdown\nhello", session_id="chat-9")

    assert created["action"] == "create"
    assert dt.get_active_document("chat-9") == created["doc_id"]
    assert dt.get_active_document("chat-10") is None
    assert f"#document-{created['doc_id']}" in created["document_summary"]


# ---------------------------------------------------------------------------
# Approval path
# ---------------------------------------------------------------------------

def test_approval_sealed_target_is_used_and_must_match_explicit_ref(db):
    sealed = _add_doc(db, age_minutes=30)
    other = _add_doc(db, age_minutes=0)
    dt.set_active_document(other, "chat-1")

    def ctx(**extra):
        base = {
            "owner": "alice", "session_id": "chat-1", "doc_id": sealed,
            "expected_document_version": 1,
            "expected_document_digest": dt.document_content_digest("Status: draft\nBody"),
        }
        base.update(extra)
        return base

    mismatch = asyncio.run(TOOL_HANDLERS["edit_document"](
        dt.with_document_id_header(EDIT, other), ctx(),
    ))
    assert mismatch["exit_code"] == 1
    assert _doc(db, other)[1] == 1 and _doc(db, sealed)[1] == 1

    ok = asyncio.run(TOOL_HANDLERS["edit_document"](
        dt.with_document_id_header(EDIT, f"#document-{sealed}"), ctx(),
    ))
    assert ok["doc_id"] == sealed
    assert _doc(db, sealed)[:2] == ("Status: final\nBody", 2)

    # The sealed version moved on: the approval is stale, nothing is written.
    stale = asyncio.run(TOOL_HANDLERS["edit_document"](EDIT, ctx()))
    assert stale.get("document_changed") is True
    assert _doc(db, other)[1] == 1


def test_approval_without_header_uses_sealed_doc(db):
    sealed = _add_doc(db, age_minutes=30)
    _add_doc(db, age_minutes=0)

    result = asyncio.run(TOOL_HANDLERS["update_document"]("Approved rewrite", {
        "owner": "alice", "session_id": "chat-1", "doc_id": sealed,
        "expected_document_version": 1,
        "expected_document_digest": dt.document_content_digest("Status: draft\nBody"),
    }))

    assert result["doc_id"] == sealed
    assert _doc(db, sealed)[0] == "Approved rewrite"


def test_resolve_document_for_approval_follows_explicit_id(db):
    active = _add_doc(db, title="Open doc")
    named = _add_doc(db, title="Named doc")

    class _Active:
        id = active
        version_count = 1
        current_content = "x"

    dt.set_active_document(active, "chat-1")
    assert dt.resolve_document_for_approval(EDIT, "alice", "chat-1", _Active) is _Active

    target = dt.resolve_document_for_approval(
        dt.with_document_id_header(EDIT, f"#document-{named}"), "alice", "chat-1", _Active,
    )
    assert target.id == named and target.version_count == 1

    assert dt.resolve_document_for_approval(
        dt.with_document_id_header(EDIT, "no-such-doc"), "alice", "chat-1", _Active,
    ) is None


def test_dispatch_passes_approved_doc_id_through(monkeypatch):
    import src.agent_tools as agent_tools
    import src.tool_execution as te

    seen = []

    async def _record(content, ctx):
        seen.append((content, ctx))
        return {"output": "ok", "exit_code": 0}

    monkeypatch.setitem(agent_tools.TOOL_HANDLERS, "edit_document", _record)
    asyncio.run(te._document_tool_dispatch(
        "edit_document", dt.with_document_id_header(EDIT, "doc-1"), "chat-1", "alice",
        document_id="doc-1", document_version=3, document_digest="abc",
    ))

    content, ctx = seen[0]
    assert ctx["doc_id"] == "doc-1"
    assert ctx["session_id"] == "chat-1"
    assert dt.split_document_id_header(content) == ("doc-1", EDIT)


# ---------------------------------------------------------------------------
# Plumbing: schemas, header, model-facing text, frontend
# ---------------------------------------------------------------------------

def test_schemas_offer_document_id():
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    by_name = {s["function"]["name"]: s["function"] for s in FUNCTION_TOOL_SCHEMAS}
    for name in ("edit_document", "update_document", "suggest_document"):
        params = by_name[name]["parameters"]
        assert "document_id" in params["properties"], name
        assert "document_id" not in params["required"], name


def test_header_roundtrip_and_no_header():
    assert dt.split_document_id_header("plain") == (None, "plain")
    assert dt.split_document_id_header(dt.with_document_id_header("body", None)) == (None, "body")
    wrapped = dt.with_document_id_header("line1\nline2", "[T](#document-abc-123)")
    assert wrapped == "<<<DOCUMENT_ID: abc-123>>>\nline1\nline2"
    assert dt.split_document_id_header(wrapped) == ("abc-123", "line1\nline2")
    # Without a document_id the native conversion is unchanged.
    assert _native("update_document", {"content": "x"}) == "x"


def test_text_json_tool_call_carries_document_id():
    from src.tool_parsing import parse_tool_blocks

    text = (
        '```edit_document\n'
        '<<<DOCUMENT_ID: #document-abc12345>>>\n' + EDIT + '\n```'
    )
    blocks = [b for b in parse_tool_blocks(text) if b.tool_type == "edit_document"]
    assert blocks, "fenced edit_document not parsed"
    assert dt.split_document_id_header(blocks[0].content)[0] == "abc12345"


def test_update_stream_events_drop_the_target_header():
    from src.agent_loop import _document_stream_events
    from src.tool_types import ToolBlock

    events = _document_stream_events(
        ToolBlock("update_document", dt.with_document_id_header("New body", "doc-1"))
    )
    assert events[-1] == {"type": "doc_stream_delta", "content": "New body"}


def test_model_facing_result_names_the_edited_document(db):
    from src.tool_execution import format_tool_result

    intended = _add_doc(db, age_minutes=30)
    result = _run("edit_document", dt.with_document_id_header(EDIT, intended), session_id="c")
    text = format_tool_result(f"edit_document: {result['title']}", result)

    assert f'Edited document "{TITLE}" (#document-{intended})' in text
    assert f"[{TITLE}](#document-{intended})" in text

    failure = _run("edit_document", EDIT, session_id="other-chat")
    failure_text = format_tool_result("edit_document", failure)
    assert intended in failure_text and "document_id" in failure_text
    assert '"document_candidates"' not in failure_text  # listed once, in the error
