"""Embedding reuse and count()-free Chroma queries (2026-10-02)."""
import time

import numpy as np

from src import embedding_lanes as el
from src.embedding_lanes import EmbeddingLane, query_lanes


class CountingClient:
    model = "fake"

    def __init__(self):
        self.calls = []

    def encode(self, texts, normalize_embeddings=True):
        self.calls.append(list(texts))
        return np.array([[float(len(t)), 1.0] for t in texts], dtype="float32")


class FakeCollection:
    def __init__(self, n=3, strict=False):
        self.n, self.strict = n, strict
        self.count_calls = 0
        self.query_calls = []

    def count(self):
        self.count_calls += 1
        return self.n

    def query(self, n_results, **kw):
        self.query_calls.append(n_results)
        if self.strict and n_results > self.n:
            raise ValueError("Number of requested results is greater than number of elements")
        m = min(n_results, self.n)
        return {"ids": [[f"id{i}" for i in range(m)]], "distances": [[0.1] * m],
                "metadatas": [[{}] * m], "documents": [["d"] * m]}


def _lane(coll, client=None):
    return EmbeddingLane("fastembed", client or CountingClient(), coll, "c", "fake", "u", 2, "fp")


def test_same_text_is_embedded_once():
    lane = _lane(FakeCollection())
    a = lane.encode(["hello"])
    b = lane.encode(["hello", "world"])
    assert lane.client.calls == [["hello"], ["world"]]
    assert b[0] == a[0]


def test_duplicate_texts_in_one_batch_embed_once():
    lane = _lane(FakeCollection())
    lane.encode(["x", "x", "y"])
    assert lane.client.calls == [["x", "y"]]


def test_ttl_expiry_reembeds(monkeypatch):
    lane = _lane(FakeCollection())
    lane.encode(["hello"])
    monkeypatch.setattr(el, "_ENCODE_CACHE_TTL_SECONDS", 0.0)
    lane.encode(["hello"])
    assert len(lane.client.calls) == 2


def test_different_clients_do_not_share_vectors():
    l1, l2 = _lane(FakeCollection()), _lane(FakeCollection())
    l1.encode(["hello"])
    l2.encode(["hello"])
    assert len(l1.client.calls) == len(l2.client.calls) == 1


def test_query_lanes_does_not_count_and_encodes_once_per_text():
    coll = FakeCollection(n=5)
    lane = _lane(coll)
    for _ in range(3):
        out = query_lanes([lane], "q", n_results=lambda l: 4, include=["distances"])
        assert len(out) == 1
    assert coll.count_calls == 0
    assert len(lane.client.calls) == 1


def test_oversized_n_results_retries_clamped_after_error():
    coll = FakeCollection(n=2, strict=True)
    out = query_lanes([_lane(coll)], "q", n_results=lambda l: 10, include=["distances"])
    assert coll.query_calls == [10, 2]
    assert len(out[0][1]["ids"][0]) == 2


def test_empty_collection_contributes_nothing():
    for strict in (False, True):
        coll = FakeCollection(n=0, strict=strict)
        assert query_lanes([_lane(coll)], "q", n_results=lambda l: 5, include=["distances"]) == []


def test_backend_failure_is_not_reported_as_empty():
    class Dead(FakeCollection):
        def query(self, n_results, **kw):
            raise ConnectionError("down")

        def count(self):
            raise ConnectionError("down")

    import pytest
    with pytest.raises(RuntimeError):
        query_lanes([_lane(Dead())], "q", n_results=lambda l: 5, include=[], raise_if_all_failed=True)


def test_lane_query_helper_empty_result_shape():
    coll = FakeCollection(n=0, strict=True)
    res = _lane(coll).query(3, query_embeddings=[[0.0, 1.0]], include=["distances"])
    assert res["ids"] == [[]] and res["distances"] == [[]]


def test_builtin_tools_are_not_reembedded_when_unchanged(monkeypatch):
    from src import tool_index as ti

    class Coll:
        def __init__(self):
            self.docs = {}

        def get(self, where=None, include=None):
            return {"ids": list(self.docs), "documents": list(self.docs.values())}

        def delete(self, ids):
            for i in ids:
                self.docs.pop(i, None)

        def upsert(self, ids, documents, embeddings, metadatas):
            self.docs.update(zip(ids, documents))

    monkeypatch.setattr(ti, "BUILTIN_TOOL_DESCRIPTIONS", {"a": "alpha", "b": "beta"})
    idx = ti.ToolIndex.__new__(ti.ToolIndex)
    idx._lanes = [_lane(Coll())]
    idx._healthy = True
    idx.index_builtin_tools()
    first = len(idx._lanes[0].client.calls)
    assert first == 1
    el.clear_encode_cache()
    idx.index_builtin_tools()
    assert len(idx._lanes[0].client.calls) == first
    monkeypatch.setattr(ti, "BUILTIN_TOOL_DESCRIPTIONS", {"a": "alpha", "b": "beta v2"})
    idx.index_builtin_tools()
    assert idx._lanes[0].client.calls[-1] == ["Tool: b\nbeta v2"]
