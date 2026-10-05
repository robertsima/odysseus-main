"""Document retrieval does not run for turns with no content in them.

The personal-document search is two ChromaDB collections and four round trips.
It ran for "hey" and "thanks!" too, adding latency and injecting whatever a
greeting sat nearest to in embedding space.
"""
from types import SimpleNamespace

import pytest

from src.chat_processor import ChatProcessor


def _retrieval(message):
    searches = []

    def search(query, k, owner=None, allow_private=False):
        searches.append(query)
        return [{"document": "Panels face south.", "similarity": 0.95,
                 "metadata": {"file_path": "/vault/solar.md", "title": "Solar"}}]

    docs = SimpleNamespace(rag_manager=SimpleNamespace(search=search))
    processor = ChatProcessor(memory_manager=None, personal_docs_manager=docs)
    _preface, sources, *_ = processor.build_context_preface(
        message, SimpleNamespace(id=None), use_memory=False, use_skills=False, allow_private=False,
    )
    return searches, sources


@pytest.mark.parametrize("message", ["hey", "lol", "thanks!"])
def test_a_contentless_turn_does_not_search_documents(message):
    searches, sources = _retrieval(message)

    assert searches == []
    assert sources == []


@pytest.mark.parametrize("message", ["fix the failing test", "ok"])
def test_a_substantive_or_bare_ack_turn_still_searches(message):
    searches, sources = _retrieval(message)

    assert searches == [message]
    assert sources
