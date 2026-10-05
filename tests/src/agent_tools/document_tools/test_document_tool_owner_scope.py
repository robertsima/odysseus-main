import asyncio
import sys
import types

import pytest

from src.agent_tools import TOOL_HANDLERS
from src.agent_tools.document_tools import (
    _owned_document_query,
    set_active_document,
)

pytestmark = pytest.mark.security


class _Column:
    def __init__(self, name):
        self.name = name

    def __eq__(self, value):
        return (self.name, "eq", value)

    def desc(self):
        return (self.name, "desc")

    def ilike(self, value):
        return (self.name, "ilike", value)


class _Document:
    id = _Column("id")
    owner = _Column("owner")
    is_active = _Column("is_active")
    title = _Column("title")
    language = _Column("language")
    updated_at = _Column("updated_at")


class _Query:
    def __init__(self, docs=None, first_doc=None):
        self.filters = []
        self.docs = docs or []
        self.first_doc = first_doc

    def filter(self, *clauses):
        self.filters.extend(clauses)
        return self

    def order_by(self, *args):
        return self

    def limit(self, *args):
        return self

    def all(self):
        return self.docs

    def first(self):
        return self.first_doc


class _Db:
    def __init__(self, query):
        self.query_obj = query

    def query(self, *args):
        return self.query_obj

    def close(self):
        pass


def _install_database_stub(monkeypatch, module_name, query):
    db = _Db(query)
    db_mod = types.ModuleType(module_name)
    db_mod.SessionLocal = lambda: db
    db_mod.Document = _Document
    db_mod.DocumentVersion = object
    db_mod.Session = object
    monkeypatch.setitem(sys.modules, module_name, db_mod)
    return db


def test_owned_document_query_rejects_missing_owner():
    query = _Query()

    assert _owned_document_query(query, _Document, None) is query
    assert False in query.filters


def test_owned_document_query_filters_to_owner():
    query = _Query()

    assert _owned_document_query(query, _Document, "alice") is query
    assert ("owner", "eq", "alice") in query.filters


def test_manage_documents_list_filters_to_calling_owner(monkeypatch):
    query = _Query()
    _install_database_stub(monkeypatch, "core.database", query)

    result = asyncio.run(
        TOOL_HANDLERS["manage_documents"]('{"action":"list"}', {"owner": "alice"})
    )

    assert result["documents"] == []
    assert ("owner", "eq", "alice") in query.filters


def test_manage_documents_read_filters_to_calling_owner(monkeypatch):
    query = _Query()
    _install_database_stub(monkeypatch, "core.database", query)

    result = asyncio.run(
        TOOL_HANDLERS["manage_documents"](
            '{"action":"read","document_id":"doc-bob"}', {"owner": "alice"}
        )
    )

    assert result["exit_code"] == 1
    assert ("id", "eq", "doc-bob") in query.filters
    assert ("owner", "eq", "alice") in query.filters


def test_update_document_active_id_filters_to_calling_owner(monkeypatch):
    query = _Query()
    _install_database_stub(monkeypatch, "src.database", query)
    set_active_document("doc-bob")
    try:
        result = asyncio.run(
            TOOL_HANDLERS["update_document"]("new content", {"owner": "alice"})
        )
    finally:
        set_active_document(None)

    # The pointer names a document alice does not own: refused, never
    # replaced by some other document.
    assert result["exit_code"] == 1
    assert "doc-bob" in result["error"]
    assert result.get("needs_document_id") is True
    assert ("id", "eq", "doc-bob") in query.filters
    assert ("owner", "eq", "alice") in query.filters


def test_suggest_document_active_id_filters_to_calling_owner(monkeypatch):
    query = _Query()
    _install_database_stub(monkeypatch, "src.database", query)
    set_active_document("doc-bob")
    try:
        result = asyncio.run(
            TOOL_HANDLERS["suggest_document"](
                "<<<FIND>>>\nold\n<<<SUGGEST>>>\nnew\n<<<REASON>>>\nbetter\n<<<END>>>",
                {"owner": "alice"},
            )
        )
    finally:
        set_active_document(None)

    assert result["exit_code"] == 1
    assert "doc-bob" in result["error"]
    assert ("id", "eq", "doc-bob") in query.filters
    assert ("owner", "eq", "alice") in query.filters


_DOCUMENT_TOOLS = (
    "create_document", "update_document", "edit_document",
    "suggest_document", "manage_documents",
)


@pytest.mark.parametrize("tool", _DOCUMENT_TOOLS)
def test_document_tool_dispatch_forwards_owner(monkeypatch, tool):
    """The agent's tool path must hand document tools the calling owner.

    The owner filters above only protect anything if execute_tool_block — the
    single entry point the agent loop dispatches through — puts the caller's
    owner (and session) into the handler ctx. Driven end to end with the
    registry handler swapped for a recorder, so a refactor of the dispatch
    helper cannot drop the owner unnoticed.
    """
    # Resolve both modules live: other test modules pop and re-import
    # src.tool_execution, so a top-level reference could be a stale copy.
    import src.agent_tools as agent_tools
    import src.tool_execution as te

    assert callable(agent_tools.TOOL_HANDLERS.get(tool)), f"TOOL_HANDLERS missing {tool!r}"

    seen = []

    async def _record(content, ctx):
        seen.append(ctx)
        return {"output": "ok", "exit_code": 0}

    monkeypatch.setitem(agent_tools.TOOL_HANDLERS, tool, _record)
    # manage_documents is admin-gated for non-admins; the gate is covered
    # elsewhere. Pass it here so every document tool reaches dispatch — an
    # admin's documents are still owner-scoped, so the owner must still arrive.
    monkeypatch.setattr(te, "_owner_is_admin", lambda owner: True)

    _desc, result = asyncio.run(te.execute_tool_block(
        agent_tools.ToolBlock(tool, '{"action":"list"}'),
        session_id="sess-alice",
        owner="alice",
        security_context=te.NO_TOOL_SECURITY_CONTEXT,
    ))

    assert result.get("exit_code") == 0, result
    assert len(seen) == 1, f"{tool} did not reach its registry handler"
    assert seen[0]["owner"] == "alice"
    assert seen[0]["session_id"] == "sess-alice"
