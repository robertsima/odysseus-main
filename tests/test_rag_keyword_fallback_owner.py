"""``VectorRAG._keyword_search_fallback`` no longer filters by owner at all.

Until 2026-09-04 the primary hybrid search filtered with ChromaDB
``where={"owner": owner}``, and this file tested that the keyword fallback
(used when that primary path errors) re-implemented the same scoping so an
owner-less document couldn't leak across users when it did.

That scoping was removed instead: no indexing path (``index_file`` /
``index_personal_documents``) has ever stamped an ``owner`` key on a chunk on
this single-user deployment, so the equality filter excluded every chunk in
the vault, always — confirmed by inspecting live chunk metadata, none of
which carried an ``owner`` key. ``owner`` is still accepted by both the
primary search and this fallback (see ``rag_vector._build_where``) so a
future multi-user deployment that actually stamps it at write time can
re-enable scoping without hunting down call sites again, but today it is a
no-op everywhere. See [[chroma-and-fastembed-are-layers]] /
docs/vault-retrieval.md for the rest of the retrieval-scoping picture.
"""
from src.rag_vector import VectorRAG


class _FakeCollection:
    def __init__(self, docs):
        # docs: list of (id, text, metadata)
        self._docs = docs

    def count(self):
        return len(self._docs)

    def get(self, include=None):
        return {
            "ids": [d[0] for d in self._docs],
            "documents": [d[1] for d in self._docs],
            "metadatas": [d[2] for d in self._docs],
        }


def _store(docs):
    store = VectorRAG.__new__(VectorRAG)
    store._collection = _FakeCollection(docs)
    return store


def test_owner_param_no_longer_filters():
    store = _store([
        ("a", "alice secret project", {"owner": "alice"}),
        ("b", "bob secret project", {"owner": "bob"}),
        ("c", "ownerless secret project", {}),          # no owner key
    ])
    results = store._keyword_search_fallback("secret project", k=10, owner="alice")
    ids = {r["id"] for r in results}
    assert ids == {"a", "b", "c"}   # owner is ignored; nothing is excluded by it


def test_no_owner_filter_returns_all():
    store = _store([
        ("a", "shared note", {"owner": "alice"}),
        ("c", "shared note", {}),
    ])
    results = store._keyword_search_fallback("shared note", k=10, owner=None)
    ids = {r["id"] for r in results}
    assert ids == {"a", "c"}
