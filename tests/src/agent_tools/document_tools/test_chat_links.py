"""manage_documents links each document by its id.

The chat renders `[Title](#document-<id>)` as a link that opens the editor,
and the editor loads GET /api/document/<id>, which is keyed by the document's
UUID. A link built from the title or a slug 404s when clicked (#560).
"""
import asyncio
import json
import uuid

import pytest


@pytest.fixture
def manage_documents(monkeypatch, app_db):
    import core.database as core_database
    import src.agent_tools.document_tools as document_tools

    monkeypatch.setattr(core_database, "SessionLocal", app_db.SessionLocal)

    def run(args):
        return asyncio.run(document_tools.ManageDocumentTool().execute(json.dumps(args), {"owner": "alice"}))

    return run


@pytest.fixture
def doc_id(app_db):
    from core.database import Document

    doc_id = str(uuid.uuid4())
    session = app_db.SessionLocal()
    try:
        session.add(Document(
            id=doc_id, title="Quarterly plan", language="markdown",
            current_content="Body", version_count=1, is_active=True, owner="alice",
        ))
        session.commit()
    finally:
        session.close()
    return doc_id


def test_the_list_links_each_document_by_id(manage_documents, doc_id):
    result = manage_documents({"action": "list"})

    assert result["exit_code"] == 0
    assert f"[Quarterly plan](#document-{doc_id})" in result["response"]


def test_opening_a_document_links_it_by_id(manage_documents, doc_id):
    result = manage_documents({"action": "open", "document_id": doc_id})

    assert result["exit_code"] == 0
    assert f"[Quarterly plan](#document-{doc_id})" in result["response"]
