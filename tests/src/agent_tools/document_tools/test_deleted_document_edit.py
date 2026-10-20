"""Editing a deleted document says it was deleted.

2026-10-20: a contract the agent had edited at 20:14 was deleted by 20:22. The
document APIs still returned it, but edit_document said "not found among your
documents", and the agent spent 37 rounds reading the app's source to find out
why. The error now names the deletion and hands back the last text.
"""

import asyncio
import json

import pytest

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

import src.agent_tools.document_tools as dt  # noqa: E402
from src.agent_tools import TOOL_HANDLERS  # noqa: E402

pytestmark = pytest.mark.security


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


def _add(SessionLocal, doc_id, owner, *, active):
    from src.database import Document

    s = SessionLocal()
    try:
        s.add(Document(id=doc_id, session_id=None, title="Deliverable Agreement", language="markdown",
                       current_content="Client: [CLIENT LEGAL NAME]\n", version_count=1,
                       is_active=active, owner=owner))
        s.commit()
    finally:
        s.close()


def _edit(doc_id, owner):
    from src.tool_schemas import function_call_to_tool_block

    block = function_call_to_tool_block("edit_document", json.dumps({
        "document_id": doc_id,
        "edits": [{"find": "[CLIENT LEGAL NAME]", "replace": "DonoFuel LLC"}],
    }))
    return asyncio.run(TOOL_HANDLERS["edit_document"](block.content, {"owner": owner, "session_id": "c"}))


def test_a_deleted_document_is_named_as_deleted_with_its_text(db):
    _add(db, "doc-deleted-1", "alice", active=False)
    result = _edit("doc-deleted-1", "alice")

    assert result["exit_code"] == 1
    assert "was deleted" in result["error"]
    assert "create_document" in result["error"]
    assert result["deleted_document_content"] == "Client: [CLIENT LEGAL NAME]\n"


def test_another_owners_deleted_document_stays_not_found(db):
    _add(db, "doc-deleted-2", "bob", active=False)
    result = _edit("doc-deleted-2", "alice")

    assert result["exit_code"] == 1
    assert "was not found among your documents" in result["error"]
    assert "deleted_document_content" not in result
