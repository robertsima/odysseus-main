"""The index says when it describes files that are no longer there.

2026-09-21: every declared personal directory was "not a directory —
skipped" (mount absent) while ChromaDB still returned >1,000 chunks from those
trees, and a planning agent presented them as the current vault.
"""

import asyncio
import os
from types import SimpleNamespace

from src import retrieval_health as rh


class _Collection:
    def __init__(self, metas):
        self._metas = metas

    def get(self, include=None, limit=None, offset=0):
        rows = self._metas[offset:offset + limit] if limit else self._metas
        return {"metadatas": rows}


class _Rag:
    healthy = True

    def __init__(self, metas):
        self._c = _Collection(metas)

    def _active_collections(self):
        return [("fastembed", self._c)]

    def get_stats(self):
        return {"document_count": len(self._c._metas), "embedding_lanes": [], "persist_directory": "/chroma"}


def _tree(tmp_path):
    vault = tmp_path / "vault"
    (vault / "AI Mind").mkdir(parents=True)
    note = vault / "AI Mind" / "plan.md"
    note.write_text("x")
    return vault, note


def test_missing_mount_with_vectors_is_stale_and_untrustworthy(tmp_path, monkeypatch):
    vault, note = _tree(tmp_path)
    journal = vault / "Journal"  # declared, never mounted
    monkeypatch.setenv("ODYSSEUS_PERSONAL_DIRS", "Journal:private")
    manager = SimpleNamespace(personal_dir=str(vault), indexed_directories=[str(vault / "AI Mind")],
                              directory_sensitivity={}, index=[])
    rag = _Rag([
        {"source": str(note)},
        {"source": str(journal / "2026-09-01.md")},
        {"source": str(journal / "2026-09-02.md")},
    ])
    report = rh.retrieval_health(manager, rag, allow_private=False)
    assert report["status"] == "degraded" and not report["current_context_trustworthy"]
    rows = {r["directory"]: r for r in report["sources"]}
    assert rows[os.path.abspath(str(vault / "AI Mind"))]["status"] == "ok"
    private = rows["(private source)"]
    assert private["status"] == "stale_vectors" and private["chunk_count"] == 2 and not private["exists"]
    assert any("a private source has 2 indexed chunk(s) but its directory is missing" in p
               for p in report["problems"])
    # A private path is disclosure; it is not named without the grant.
    assert str(journal) not in repr(report)


def test_everything_present_and_indexed_is_healthy(tmp_path, monkeypatch):
    monkeypatch.delenv("ODYSSEUS_PERSONAL_DIRS", raising=False)
    vault, note = _tree(tmp_path)
    manager = SimpleNamespace(personal_dir=str(vault), indexed_directories=[], directory_sensitivity={}, index=[])
    report = rh.retrieval_health(manager, _Rag([{"source": str(note)}]))
    assert report["status"] == "healthy" and report["current_context_trustworthy"]
    assert report["problems"] == []


def test_files_with_no_vectors_are_not_indexed(tmp_path, monkeypatch):
    monkeypatch.delenv("ODYSSEUS_PERSONAL_DIRS", raising=False)
    vault, _note = _tree(tmp_path)
    manager = SimpleNamespace(personal_dir=str(vault), indexed_directories=[], directory_sensitivity={}, index=[])
    report = rh.retrieval_health(manager, _Rag([]), allow_private=True)
    assert report["sources"][0]["status"] == "not_indexed"
    assert not report["current_context_trustworthy"]


def test_no_vector_store_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv("ODYSSEUS_PERSONAL_DIRS", raising=False)
    vault, _ = _tree(tmp_path)
    manager = SimpleNamespace(personal_dir=str(vault), indexed_directories=[], directory_sensitivity={}, index=[])
    report = rh.retrieval_health(manager, None)
    assert report["status"] == "unavailable"
    assert "the vector store is not available" in report["problems"]


def test_search_documents_carries_the_warning(monkeypatch):
    from src.agent_tools import rag_tools

    class _SearchRag:
        def search(self, *a, **k):
            return [{"document": "Old plan", "similarity": 0.9, "metadata": {"source": "/vault/Journal/a.md"}}]

    monkeypatch.setattr("src.rag_singleton.get_rag_manager", lambda: _SearchRag())
    monkeypatch.setattr(rh, "cached_problems", lambda: ["a private source has 12 indexed chunk(s) but its directory is missing"])
    result = asyncio.run(rag_tools.SearchDocumentsTool().execute('{"query": "plan"}', {"owner": "u"}))
    assert result["index_health"]
    assert result["results"].startswith("INDEX HEALTH WARNING")
    monkeypatch.setattr(rh, "cached_problems", lambda: [])
    clean = asyncio.run(rag_tools.SearchDocumentsTool().execute('{"query": "plan"}', {"owner": "u"}))
    assert "index_health" not in clean and not clean["results"].startswith("INDEX HEALTH")
