"""Legacy unsuffixed Chroma collections are left alone.

This module used to assert the opposite: that a pre-lane `odysseus_memories`
collection was read at startup and backfilled into each lane, resuming a
partial backfill and continuing past a lane that failed. Commit a43bcb0
("fix: route tools and simplify retrieval runtime") removed that migration --
`migrate_legacy_collection()` is a documented no-op and
`specs/retrieval-runtime.md` states that legacy collections "are not queried or
migrated". Those four backfill tests were deleted with the behaviour rather
than lost; what is asserted here now is the contract that replaced them.
"""

from tests.helpers.embedding_lanes import FakeChroma, FakeEmbedder, patch_chroma


class ReadTrackingChroma(FakeChroma):
    def __init__(self):
        super().__init__()
        self.reads = []

    def get_collection(self, name):
        self.reads.append(name)
        return super().get_collection(name)


def test_migrate_legacy_collection_touches_nothing(monkeypatch):
    import src.embedding_lanes as lanes

    fake = ReadTrackingChroma()
    patch_chroma(monkeypatch, fake)
    legacy = fake.get_or_create_collection("odysseus_memories", metadata={"hnsw:space": "cosine"})
    legacy.add(ids=["legacy-memory"], embeddings=[[0.0] * 384], documents=["legacy memory row"])
    lane_collection = fake.get_or_create_collection("odysseus_memories_fastembed")
    lane = type("Lane", (), {"collection": lane_collection, "name": "fastembed"})()

    lanes.migrate_legacy_collection("odysseus_memories", [lane])

    assert fake.reads == []
    assert fake.deleted == []
    assert lane_collection.count() == 0
    assert legacy.count() == 1


def test_store_startup_does_not_read_or_backfill_the_legacy_collection(monkeypatch):
    fake = ReadTrackingChroma()
    legacy = fake.get_or_create_collection("odysseus_memories", metadata={"hnsw:space": "cosine"})
    legacy.add(
        ids=["legacy-memory"],
        embeddings=[[0.0] * 384],
        documents=["legacy memory row"],
        metadatas=[{"source": "memory"}],
    )
    patch_chroma(monkeypatch, fake)

    import src.embedding_lanes as lanes

    monkeypatch.setattr(lanes, "_build_fastembed_client", lambda: FakeEmbedder(384, "mini", "local://fastembed"))

    from src.memory_vector import MemoryVectorStore

    store = MemoryVectorStore("data")

    assert store.healthy
    assert "odysseus_memories" not in fake.reads
    assert fake.collections["odysseus_memories"].count() == 1
    assert fake.collections["odysseus_memories_fastembed"].count() == 0
    assert store.count() == 0
