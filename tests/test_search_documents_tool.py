"""Tests for the search_documents agent tool.

The point of this tool is to stop the agent pulling whole vault notes into
context, so the tests that matter are: it is actually reachable as a tool, it
never returns private material to a non-local endpoint, and it degrades to a
readable message instead of an exception when ChromaDB is down.
"""

import asyncio

import pytest

import src.agent_tools  # noqa: F401  — import first; resolves the tool_schemas cycle
from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
from src.agent_tools.rag_tools import (
    SearchDocumentsTool,
    _clamp_k,
    _parse_args,
    _resolve_allow_private,
    _source_lines,
)
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS


def _schema():
    for entry in FUNCTION_TOOL_SCHEMAS:
        fn = entry.get("function") or {}
        if fn.get("name") == "search_documents":
            return fn
    return None


def test_tool_is_registered_on_every_gate():
    # A tool missing from any one of these is silently unreachable: the model
    # either never sees it, or its call is rejected as an unknown function.
    assert "search_documents" in TOOL_HANDLERS
    assert "search_documents" in TOOL_TAGS
    assert _schema() is not None


def test_schema_shape():
    fn = _schema()
    params = fn["parameters"]
    assert params["required"] == ["query"]
    assert set(params["properties"]) == {"query", "k"}
    # The description is what steers the model away from read_file; if it stops
    # naming the alternative, the tool stops being chosen for the right reason.
    assert "read" in fn["description"].lower()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("what did I write about burnout", {"query": "what did I write about burnout"}),
        ('{"query": "burnout", "k": 3}', {"query": "burnout", "k": 3}),
        ("{not valid json", {"query": "{not valid json"}),
        ("", {}),
    ],
)
def test_parse_args(raw, expected):
    assert _parse_args(raw) == expected


@pytest.mark.parametrize(
    "value,expected",
    [(999, 12), (-1, 1), (0, 1), ("x", 5), (None, 5), (4, 4)],
)
def test_clamp_k(value, expected):
    assert _clamp_k(value) == expected


def test_empty_query_is_rejected_before_touching_the_index():
    result = asyncio.run(SearchDocumentsTool().execute("   ", {}))
    assert "error" in result
    assert result["exit_code"] == 1


def test_private_retrieval_fails_closed():
    # Retrieved text is pasted into the outbound prompt. An unresolvable
    # session must yield public-only results rather than defaulting to trust.
    assert _resolve_allow_private(None) is False
    assert _resolve_allow_private("no-such-session-id") is False


def test_source_lines_dedupe_by_path():
    results = [
        {"metadata": {"source": "/vault/a.md", "filename": "a.md"}, "similarity": 0.9},
        {"metadata": {"source": "/vault/a.md", "filename": "a.md"}, "similarity": 0.7},
        {"metadata": {"source": "/vault/b.md", "filename": "b.md"}, "similarity": 0.5},
    ]
    lines = _source_lines(results)
    assert len(lines) == 2
    assert any("/vault/a.md" in line for line in lines)
    assert any("/vault/b.md" in line for line in lines)


def test_missing_index_degrades_to_a_message(monkeypatch):
    import src.rag_singleton as singleton

    monkeypatch.setattr(singleton, "get_rag_manager", lambda: None)
    result = asyncio.run(
        SearchDocumentsTool().execute("burnout", {"owner": "someone", "session_id": None})
    )
    assert "error" in result
    assert "not available" in result["error"]


def test_results_are_bounded_and_cite_sources(monkeypatch):
    """A search must never cost what reading the files would have cost."""
    import src.rag_singleton as singleton

    huge = "x " * 40000  # ~80k chars, i.e. a whole vault note

    class FakeRag:
        def search(self, query, k, owner=None, allow_private=True):
            return [
                {
                    "document": huge,
                    "metadata": {"source": f"/vault/note{i}.md", "filename": f"note{i}.md"},
                    "similarity": 0.9,
                }
                for i in range(3)
            ]

    monkeypatch.setattr(singleton, "get_rag_manager", lambda: FakeRag())
    result = asyncio.run(
        SearchDocumentsTool().execute("burnout", {"owner": "someone", "session_id": None})
    )
    assert "results" in result
    body = result["results"]
    # Three 80k-char notes would be 240k chars raw; the renderer shares a fixed
    # budget across them, so the result stays in the low thousands.
    assert len(body) < 12000, f"unbounded result: {len(body)} chars"
    assert "/vault/note0.md" in body
