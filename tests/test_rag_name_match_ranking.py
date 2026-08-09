"""Naming a document in the query has to actually surface it.

Putting the filename into chunk text (see test_rag_chunk_filename_header) made
the date *present*, but not *findable*: the keyword half of the hybrid score
divides matches by query length, so the one token identifying the file was
worth 1/13 of the keyword weight while a competing entry won on raw vector
similarity. These pin the ranking behaviour, not just the helper.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import src.rag_vector as rag_vector
from src.rag_vector import VectorRAG, _query_names_document

QUERY = "can you tell me about what i wrote in my journal on 08-08-2026"

# The entry actually asked for. Its prose never mentions its own date — only
# the header carries it — and it is the weaker vector match of the two.
WANTED = {
    "id": "wanted",
    "document": "Source: 08-08-2026.md 08-08-2026\nI haven't written here in a while.",
    "metadata": {"filename": "08-08-2026.md", "source": "/v/08-08-2026.md"},
    "distance": 0.45,
}

# A different entry that simply reads more like the question.
COMPETITOR = {
    "id": "competitor",
    "document": "Source: 05-21-2025.md 05-21-2025\nI wrote about what my journal means to me.",
    "metadata": {"filename": "05-21-2025.md", "source": "/v/05-21-2025.md"},
    "distance": 0.35,
}


class _Lane:
    name = "test"

    def count(self):
        return 100


def _fake_search(monkeypatch, rows):
    results = {
        "ids": [[r["id"] for r in rows]],
        "distances": [[r["distance"] for r in rows]],
        "documents": [[r["document"] for r in rows]],
        "metadatas": [[r["metadata"] for r in rows]],
    }
    monkeypatch.setattr(rag_vector, "query_lanes", lambda *a, **k: [(_Lane(), results)])
    monkeypatch.setattr(rag_vector, "lane_count", lambda lanes: 1)

    rag = VectorRAG.__new__(VectorRAG)
    rag._lanes = [_Lane()]
    rag._healthy = True
    return rag


def test_named_entry_outranks_a_stronger_vector_match(monkeypatch):
    rag = _fake_search(monkeypatch, [COMPETITOR, WANTED])

    results = rag.search(QUERY, k=5)

    assert results[0]["metadata"]["filename"] == "08-08-2026.md", (
        "the entry the query names must come first even though the other "
        "candidate is the closer vector match"
    )


def test_without_the_name_the_vector_match_still_wins(monkeypatch):
    # No regression: a query that names nothing is ordered exactly as before.
    rag = _fake_search(monkeypatch, [COMPETITOR, WANTED])

    results = rag.search("what have i been journalling about lately", k=5)

    assert results[0]["metadata"]["filename"] == "05-21-2025.md"


def test_year_alone_still_names_the_entry(monkeypatch):
    rag = _fake_search(monkeypatch, [COMPETITOR, WANTED])

    results = rag.search("anything from 2026 in my journal", k=5)

    assert results[0]["metadata"]["filename"] == "08-08-2026.md"


# -- what counts as naming a document -------------------------------------


def test_two_character_fragments_do_not_count():
    # "08-08-2026" splits to {08, 08, 2026}; an "08" match would otherwise
    # fire for any eighth-of-the-month or August entry.
    assert _query_names_document({"08"}, {"filename": "08-08-2026.md"}) is False


def test_common_words_do_not_count():
    assert _query_names_document({"notes"}, {"filename": "notes.md"}) is False
    assert _query_names_document({"readme"}, {"filename": "README.md"}) is False


def test_prose_filenames_still_match_on_a_real_word():
    assert _query_names_document(
        {"architecture"}, {"filename": "architecture.md"}
    ) is True


@pytest.mark.parametrize("meta", [None, {}, {"filename": ""}, {"filename": 5}, "nope"])
def test_missing_or_malformed_metadata_is_not_a_match(meta):
    assert _query_names_document({"anything"}, meta) is False


def test_empty_query_is_not_a_match():
    assert _query_names_document(set(), {"filename": "08-08-2026.md"}) is False
