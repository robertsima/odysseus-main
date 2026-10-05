"""Memory Tidy must not issue one ChromaDB round-trip per memory.

Production logs showed the 'Memory Tidy' task (consolidate_memory) doing one
``POST .../collections/<id>/get`` per memory (~125 sequential calls) after a
run that removed one duplicate: the vector-index resync called
``MemoryVectorStore.add(id, text)`` for every surviving memory, and each
``add`` did its own ``get(ids=[id])`` existence check. It also re-probed the
already-active fastembed lane collection on every delete. The resync now uses
``remove_many`` / ``add_many``: one delete and one (paged) existence ``get``
per collection.
"""
import asyncio
import hashlib
from unittest.mock import MagicMock

import pytest

import src.builtin_actions as ba
from src.memory_vector import MemoryVectorStore


class _FakeLane:
    def __init__(self, name, collection):
        self.name = name
        self.collection = collection

    def encode(self, texts):
        return [[0.0, 1.0] for _ in texts]


def _collection(name, present_ids=()):
    col = MagicMock()
    col.name = name
    present = set(present_ids)

    def _get(ids=None, include=None, **_kw):
        return {"ids": [i for i in (ids or []) if i in present]}

    col.get.side_effect = _get
    return col


def _store(*lanes):
    store = MemoryVectorStore.__new__(MemoryVectorStore)
    store._model = None
    store._lanes = list(lanes)
    store._collection = lanes[0].collection if lanes else None
    store._healthy = True
    return store


@pytest.fixture
def chroma_client(monkeypatch):
    """Fake client: the inactive custom lane was never built (404)."""
    import src.chroma_client

    client = MagicMock()
    probed = []

    def _get_collection(name):
        probed.append(name)
        raise Exception("Collection odysseus_memories_custom does not exist.")

    client.get_collection.side_effect = _get_collection
    client.probed = probed
    monkeypatch.setattr(src.chroma_client, "get_chroma_client", lambda: client)
    return client


def test_add_many_does_one_existence_get_per_lane():
    col = _collection("odysseus_memories_fastembed", present_ids={f"m{i}" for i in range(125)})
    store = _store(_FakeLane("fastembed", col))

    added = store.add_many((f"m{i}", f"text {i}") for i in range(125))

    assert added == 0
    assert col.get.call_count == 1
    assert col.get.call_args.kwargs["ids"] == [f"m{i}" for i in range(125)]
    col.add.assert_not_called()


def test_add_many_adds_only_missing_ids_and_pages_large_batches():
    ids = [f"m{i}" for i in range(1200)]
    col = _collection("odysseus_memories_fastembed", present_ids=set(ids[:1000]))
    store = _store(_FakeLane("fastembed", col))

    added = store.add_many([(i, f"text {i}") for i in ids] + [("m1100", "dup id, first wins")])

    assert added == 200
    # 1200 ids / 500 per page -> 3 gets, not 1200.
    assert col.get.call_count == 3
    added_ids = [i for call in col.add.call_args_list for i in call.kwargs["ids"]]
    assert added_ids == ids[1000:]
    first_batch = col.add.call_args_list[0].kwargs
    assert first_batch["documents"][0] == "text m1000"


def test_remove_many_one_delete_and_no_reprobe_of_active_lane(chroma_client):
    col = _collection("odysseus_memories_fastembed")
    store = _store(_FakeLane("fastembed", col))

    store.remove_many(["a", "b", "a"])

    col.delete.assert_called_once_with(ids=["a", "b"])
    # The active fastembed lane is already known; only the inactive custom
    # lane is probed (once, not once per removed id).
    assert chroma_client.probed == ["odysseus_memories_custom"]


class _FakeMM:
    memories = []
    saved = None

    def __init__(self, *args, **kwargs):
        pass

    def load_all(self):
        return [dict(m) for m in _FakeMM.memories]

    def save(self, entries):
        _FakeMM.saved = list(entries)


def test_memory_tidy_resyncs_vector_index_in_batches(monkeypatch, chroma_client):
    import src.ai_interaction
    import src.memory
    import src.task_endpoint

    memories = [
        {
            "id": f"m{i}",
            "owner": "alice",
            "category": "fact",
            "text": f"Fact {hashlib.sha1(str(i).encode()).hexdigest()} "
                    f"{hashlib.md5(str(i).encode()).hexdigest()}",
        }
        for i in range(125)
    ]
    memories.append(dict(memories[0], id="dup"))  # exact duplicate of m0
    _FakeMM.memories = memories
    _FakeMM.saved = None

    col = _collection("odysseus_memories_fastembed", present_ids={m["id"] for m in memories})
    store = _store(_FakeLane("fastembed", col))

    monkeypatch.setattr(src.memory, "MemoryManager", _FakeMM)
    monkeypatch.setattr(src.task_endpoint, "resolve_task_candidates", lambda owner=None: [])
    monkeypatch.setattr(src.ai_interaction, "_memory_vector", store)

    msg, ok = asyncio.run(ba.action_consolidate_memory("alice"))

    assert ok, msg
    assert len(_FakeMM.saved) == 125
    gone = ({m["id"] for m in memories} - {m["id"] for m in _FakeMM.saved})
    assert len(gone) == 1
    col.delete.assert_called_once_with(ids=sorted(gone))
    # One batched existence check instead of ~125 sequential /get calls.
    assert col.get.call_count == 1
    col.add.assert_not_called()
    assert chroma_client.probed == ["odysseus_memories_custom"]
