"""A named document must be retrieved, not merely re-ranked once retrieved.

The vector pass returns only the ~20 nearest chunks by embedding. A journal
entry named for its date is, in prose, about whatever happened that day, so a
question naming the date lands nowhere near it and the file never enters the
pool — and no amount of re-ranking rescues a document that was never fetched.
These cover the direct lookup that puts it there.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import src.rag_vector as rag_vector
from src.rag_vector import VectorRAG, _distinctive_query_tokens

QUERY = "can you tell me about what i wrote in my journal on 08-08-2026"

# Deliberately NOT in the vector pool: this is the whole failure mode.
WANTED = {
    "id": "wanted",
    "document": "Source: 08-08-2026.md 08-08-2026\nI haven't written here in a while.",
    "metadata": {"filename": "08-08-2026.md", "source": "/v/08-08-2026.md"},
    "distance": 0.80,
}
POOL = [
    {
        "id": f"other{i}",
        "document": f"Source: 0{i}-21-2025.md 0{i}-21-2025\nI wrote about my journal.",
        "metadata": {"filename": f"0{i}-21-2025.md", "source": f"/v/0{i}-21-2025.md"},
        "distance": 0.30 + i * 0.01,
    }
    for i in range(1, 6)
]


class _Lane:
    name = "test"

    def count(self):
        return 463


def _rows_to_results(rows):
    return {
        "ids": [[r["id"] for r in rows]],
        "distances": [[r["distance"] for r in rows]],
        "documents": [[r["document"] for r in rows]],
        "metadatas": [[r["metadata"] for r in rows]],
    }


def _install(monkeypatch, corpus, pool):
    """Fake lanes: the vector pass sees `pool`, $contains searches `corpus`."""
    calls = []

    def fake_query_lanes(lanes, query, n_results, include, where=None,
                         where_document=None, raise_if_all_failed=False):
        calls.append(where_document)
        if where_document is None:
            return [(_Lane(), _rows_to_results(pool))]
        token = where_document["$contains"]
        hits = [r for r in corpus if token in r["document"]]
        return [(_Lane(), _rows_to_results(hits))] if hits else []

    monkeypatch.setattr(rag_vector, "query_lanes", fake_query_lanes)
    monkeypatch.setattr(rag_vector, "lane_count", lambda lanes: 1)

    rag = VectorRAG.__new__(VectorRAG)
    rag._lanes = [_Lane()]
    rag._healthy = True
    return rag, calls


def test_entry_outside_the_vector_pool_is_still_returned(monkeypatch):
    rag, _ = _install(monkeypatch, corpus=POOL + [WANTED], pool=POOL)

    results = rag.search(QUERY, k=5)

    names = [r["metadata"]["filename"] for r in results]
    assert "08-08-2026.md" in names, "named entry must be fetched directly"
    assert names[0] == "08-08-2026.md", "and must rank first once fetched"


def test_no_duplicate_when_the_entry_is_already_in_the_pool(monkeypatch):
    rag, _ = _install(monkeypatch, corpus=POOL + [WANTED], pool=POOL + [WANTED])

    results = rag.search(QUERY, k=5)

    ids = [r["id"] for r in results]
    assert ids.count("wanted") == 1


def test_no_lookup_when_the_query_names_nothing(monkeypatch):
    rag, calls = _install(monkeypatch, corpus=POOL + [WANTED], pool=POOL)

    rag.search("what have i been journalling about lately", k=5)

    assert calls == [None], "a query without an identifier must not spend lookups"


def test_backend_without_document_filtering_degrades_quietly(monkeypatch):
    rag, _ = _install(monkeypatch, corpus=POOL + [WANTED], pool=POOL)

    original = rag_vector.query_lanes

    def explode(*args, **kwargs):
        if kwargs.get("where_document") is not None:
            raise TypeError("where_document unsupported")
        return original(*args, **kwargs)

    monkeypatch.setattr(rag_vector, "query_lanes", explode)

    results = rag.search(QUERY, k=5)

    assert results, "ordinary results must survive an unsupported filter"


# -- which tokens earn a lookup -------------------------------------------


def test_only_identifier_shaped_tokens_are_looked_up():
    tokens = _distinctive_query_tokens(set(QUERY.split()))
    assert tokens == ["08-08-2026"]


def test_plain_words_never_earn_a_lookup():
    assert _distinctive_query_tokens({"architecture", "journal", "about"}) == []


def test_lookups_are_capped_and_longest_first():
    words = {"08-08-2026", "2026", "v2.1.4", "ticket-99"}
    tokens = _distinctive_query_tokens(words)
    assert len(tokens) <= 2
    assert tokens[0] == "08-08-2026"
