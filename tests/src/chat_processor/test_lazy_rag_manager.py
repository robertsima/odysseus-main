"""Document retrieval recovers when the vector store was not ready at startup.

The RAG manager is resolved once at import with a short ChromaDB probe. If
ChromaDB was still starting, the manager stayed None for the life of the
process and every chat turn ran without document context.
"""
from types import SimpleNamespace

import src.rag_singleton
from src.chat_processor import ChatProcessor


def test_a_turn_resolves_the_rag_manager_it_did_not_have_at_startup(monkeypatch):
    searches = []

    def search(query, k, owner=None, allow_private=False):
        searches.append(query)
        return [{"document": "Panels face south.", "similarity": 0.95,
                 "metadata": {"file_path": "/vault/solar.md", "title": "Solar"}}]

    rag = SimpleNamespace(search=search)
    monkeypatch.setattr(src.rag_singleton, "get_rag_manager", lambda: rag)
    docs = SimpleNamespace(rag_manager=None)
    processor = ChatProcessor(memory_manager=None, personal_docs_manager=docs)

    _preface, sources, *_ = processor.build_context_preface(
        "which way do my solar panels face?", SimpleNamespace(id=None),
        use_memory=False, use_skills=False, allow_private=False,
    )

    assert searches
    assert sources
    assert docs.rag_manager is rag
