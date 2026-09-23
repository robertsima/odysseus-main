"""Embedding-lane construction and collection resets.

Commit a43bcb0 ("fix: route tools and simplify retrieval runtime") collapsed
retrieval to a single local FastEmbed lane, and `specs/retrieval-runtime.md`
records that as the contract: `build_embedding_lanes()` creates exactly one
`fastembed` lane, HTTP/custom embedding endpoints are never probed or selected,
and legacy unsuffixed collections are never queried or migrated.

The tests that asserted the removed design -- the custom lane
(`_build_custom_client`), the two-lane build, the `ODYSSEUS_FASTEMBED_LANE`
switch choosing between lanes, and the legacy-collection read cache -- were
deleted with it. That coverage went deliberately; it was not lost by accident.
The behaviour that survived the collapse (lane reset on a fingerprint change,
re-embedding from stored documents, keeping or restoring the existing
collection when a rewrite fails, `primary_collection` pairing) is exercised
below through the one lane.
"""

from src.embedding_lanes import (
    LANE_FASTEMBED,
    build_embedding_lanes,
)
from tests.helpers.embedding_lanes import (
    FakeChroma,
    FakeEmbedder,
    FailingEmbedder,
    patch_chroma,
)


def _fastembed(monkeypatch, client):
    import src.embedding_lanes as lanes

    monkeypatch.setattr(lanes, "_build_fastembed_client", lambda: client)


def test_build_embedding_lanes_returns_only_the_fastembed_lane(monkeypatch):
    fake = FakeChroma()
    patch_chroma(monkeypatch, fake)
    _fastembed(monkeypatch, FakeEmbedder(384, "sentence-transformers/all-MiniLM-L6-v2", "local://fastembed"))

    built = build_embedding_lanes("odysseus_tool_index")

    assert [lane.name for lane in built] == [LANE_FASTEMBED]
    assert built[0].collection_name == "odysseus_tool_index_fastembed"
    assert built[0].dimension == 384
    assert set(fake.collections) == {"odysseus_tool_index_fastembed"}


def test_build_embedding_lanes_returns_nothing_when_fastembed_is_unavailable(monkeypatch):
    """No lane rather than another implementation: an unavailable embedder has
    to surface as degraded retrieval, not a silent switch."""
    fake = FakeChroma()
    patch_chroma(monkeypatch, fake)

    import src.embedding_lanes as lanes

    def fail_fastembed():
        raise RuntimeError("fastembed missing")

    monkeypatch.setattr(lanes, "_build_fastembed_client", fail_fastembed)

    assert build_embedding_lanes("odysseus_memories") == []
    assert fake.collections == {}


def test_lane_reset_reembeds_existing_documents_on_fingerprint_change(monkeypatch):
    fake = FakeChroma()
    stale = fake.get_or_create_collection(
        "odysseus_memories_fastembed",
        metadata={
            "embedding_lane": "fastembed",
            "embedding_dimension": 384,
            "embedding_fingerprint": "old",
        },
    )
    stale.add(
        ids=["existing-memory"],
        embeddings=[[0.0] * 384],
        documents=["existing memory"],
        metadatas=[{"source": "memory"}],
    )
    patch_chroma(monkeypatch, fake)
    _fastembed(monkeypatch, FakeEmbedder(768, "bge-large", "local://fastembed"))

    built = build_embedding_lanes("odysseus_memories")

    assert [lane.name for lane in built] == [LANE_FASTEMBED]
    assert "odysseus_memories_fastembed" in fake.deleted
    rebuilt = fake.collections["odysseus_memories_fastembed"]
    assert rebuilt.count() == 1
    assert rebuilt.get()["ids"] == ["existing-memory"]
    assert len(rebuilt.rows["existing-memory"]["embedding"]) == 768


def test_lane_reset_is_skipped_when_the_fingerprint_still_matches(monkeypatch):
    """The reset path deletes the collection, so it must run only on a real
    change -- a matching fingerprint keeps the rows and the collection."""
    fake = FakeChroma()
    patch_chroma(monkeypatch, fake)
    _fastembed(monkeypatch, FakeEmbedder(384, "mini", "local://fastembed"))

    first = build_embedding_lanes("odysseus_memories")
    first[0].collection.add(
        ids=["mem-1"],
        embeddings=first[0].encode(["a memory"]),
        documents=["a memory"],
        metadatas=[{"source": "memory"}],
    )

    second = build_embedding_lanes("odysseus_memories")

    assert fake.deleted == []
    assert second[0].fingerprint == first[0].fingerprint
    assert second[0].collection.count() == 1


def test_lane_reset_keeps_existing_collection_when_reembed_fails(monkeypatch):
    fake = FakeChroma()
    stale = fake.get_or_create_collection(
        "odysseus_memories_fastembed",
        metadata={
            "embedding_lane": "fastembed",
            "embedding_dimension": 384,
            "embedding_fingerprint": "old",
        },
    )
    stale.add(
        ids=["existing-memory"],
        embeddings=[[0.0] * 384],
        documents=["existing memory"],
        metadatas=[{"source": "memory"}],
    )
    patch_chroma(monkeypatch, fake)
    _fastembed(monkeypatch, FailingEmbedder(768, "bge-large", "local://fastembed"))

    built = build_embedding_lanes("odysseus_memories")

    assert built == []
    assert "odysseus_memories_fastembed" not in fake.deleted
    assert fake.collections["odysseus_memories_fastembed"].count() == 1
    assert len(fake.collections["odysseus_memories_fastembed"].rows["existing-memory"]["embedding"]) == 384


def test_lane_reset_keeps_existing_collection_when_preserve_read_fails(monkeypatch):
    fake = FakeChroma()
    stale = fake.get_or_create_collection(
        "odysseus_memories_fastembed",
        metadata={
            "embedding_lane": "fastembed",
            "embedding_dimension": 384,
            "embedding_fingerprint": "old",
        },
    )
    stale.add(
        ids=["existing-memory"],
        embeddings=[[0.0] * 384],
        documents=["existing memory"],
        metadatas=[{"source": "memory"}],
    )

    def fail_get(*_args, **_kwargs):
        raise RuntimeError("chroma read failed")

    stale.get = fail_get
    patch_chroma(monkeypatch, fake)
    _fastembed(monkeypatch, FakeEmbedder(768, "bge-large", "local://fastembed"))

    built = build_embedding_lanes("odysseus_memories")

    assert built == []
    assert "odysseus_memories_fastembed" not in fake.deleted
    assert "odysseus_memories_fastembed" in fake.collections


def test_lane_reset_restores_existing_collection_when_rewrite_fails(monkeypatch):
    fake = FakeChroma()
    stale = fake.get_or_create_collection(
        "odysseus_memories_fastembed",
        metadata={
            "embedding_lane": "fastembed",
            "embedding_dimension": 384,
            "embedding_fingerprint": "old",
        },
    )
    stale.add(
        ids=["existing-memory"],
        embeddings=[[0.0] * 384],
        documents=["existing memory"],
        metadatas=[{"source": "memory"}],
    )
    fake.fail_next_add_for["odysseus_memories_fastembed"] = 1
    patch_chroma(monkeypatch, fake)
    _fastembed(monkeypatch, FakeEmbedder(768, "bge-large", "local://fastembed"))

    built = build_embedding_lanes("odysseus_memories")

    assert built == []
    restored = fake.collections["odysseus_memories_fastembed"]
    assert restored.count() == 1
    assert restored.get()["ids"] == ["existing-memory"]
    assert len(restored.rows["existing-memory"]["embedding"]) == 384


def test_the_fastembed_lane_env_switch_is_inert(monkeypatch):
    """`ODYSSEUS_FASTEMBED_LANE` chose whether the local lane was built beside a
    custom one. There is no second lane to choose against any more: the value is
    still parsed, but the single lane is built either way."""
    from src.embedding_lanes import fastembed_lane_mode

    patch_chroma(monkeypatch, FakeChroma())
    _fastembed(monkeypatch, FakeEmbedder(384, "mini", "local://fastembed"))

    monkeypatch.delenv("ODYSSEUS_FASTEMBED_LANE", raising=False)
    assert fastembed_lane_mode() == "auto"
    assert [l.name for l in build_embedding_lanes("odysseus_memories")] == [LANE_FASTEMBED]

    monkeypatch.setenv("ODYSSEUS_FASTEMBED_LANE", "off")
    assert fastembed_lane_mode() == "off"
    assert [l.name for l in build_embedding_lanes("odysseus_memories")] == [LANE_FASTEMBED]


def test_primary_collection_pairs_with_the_lane_the_store_embeds_through(monkeypatch):
    """Stores keep one `_collection` and one embedder, and they have to be the
    same lane's -- embedding with one and querying the other is a dimension
    error waiting for a second lane to exist again."""
    from src.embedding_lanes import primary_collection

    patch_chroma(monkeypatch, FakeChroma())
    _fastembed(monkeypatch, FakeEmbedder(384, "mini", "local://fastembed"))

    built = build_embedding_lanes("odysseus_memories")
    assert primary_collection(built) is built[0].collection
    assert primary_collection([]) is None
