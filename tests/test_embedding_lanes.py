import pytest

from src.embedding_lanes import (
    LANE_CUSTOM,
    LANE_FASTEMBED,
    build_embedding_lanes,
)
from tests.helpers.embedding_lanes import (
    FakeChroma,
    FakeEmbedder,
    FailingEmbedder,
    patch_chroma,
)


def test_build_embedding_lanes_keeps_custom_and_fastembed_dimensions_separate(monkeypatch):
    fake = FakeChroma()
    patch_chroma(monkeypatch, fake)

    import src.embedding_lanes as lanes

    monkeypatch.setattr(
        lanes,
        "_build_custom_client",
        lambda: FakeEmbedder(768, "nomic-embed-text", "http://embeddings/v1"),
    )
    monkeypatch.setattr(
        lanes,
        "_build_fastembed_client",
        lambda: FakeEmbedder(384, "sentence-transformers/all-MiniLM-L6-v2", "local://fastembed"),
    )

    built = build_embedding_lanes("odysseus_memories")

    assert [lane.name for lane in built] == [LANE_CUSTOM, LANE_FASTEMBED]
    assert built[0].collection_name == "odysseus_memories_custom"
    assert built[0].dimension == 768
    assert built[1].collection_name == "odysseus_memories_fastembed"
    assert built[1].dimension == 384

    built[0].collection.add(ids=["custom"], embeddings=built[0].encode(["a"]), documents=["a"])
    built[1].collection.add(ids=["fast"], embeddings=built[1].encode(["a"]), documents=["a"])

    with pytest.raises(RuntimeError, match="dimension"):
        built[0].collection.query(query_embeddings=built[1].encode(["bad"]), n_results=1)


def test_build_embedding_lanes_recreates_only_custom_when_fingerprint_changes(monkeypatch):
    fake = FakeChroma()
    old_custom = fake.get_or_create_collection(
        "odysseus_rag_custom",
        metadata={
            "embedding_lane": "custom",
            "embedding_dimension": 768,
            "embedding_fingerprint": "old",
        },
    )
    old_custom.add(ids=["old"], embeddings=[[0.0] * 768], documents=["old"])
    fast = fake.get_or_create_collection(
        "odysseus_rag_fastembed",
        metadata={
            "embedding_lane": "fastembed",
            "embedding_dimension": 384,
        },
    )
    fast.add(ids=["fast"], embeddings=[[0.0] * 384], documents=["fast"])
    patch_chroma(monkeypatch, fake)

    import src.embedding_lanes as lanes

    monkeypatch.setattr(lanes, "_build_custom_client", lambda: FakeEmbedder(1024, "bge-large", "http://embeddings/v1"))
    monkeypatch.setattr(lanes, "_build_fastembed_client", lambda: FakeEmbedder(384, "sentence-transformers/all-MiniLM-L6-v2", "local://fastembed"))

    built = build_embedding_lanes("odysseus_rag")

    assert "odysseus_rag_custom" in fake.deleted
    assert fake.collections["odysseus_rag_custom"].count() == 1
    assert len(fake.collections["odysseus_rag_custom"].rows["old"]["embedding"]) == 1024
    assert fake.collections["odysseus_rag_fastembed"].count() == 1
    assert built[0].dimension == 1024


def test_lane_reset_reembeds_existing_documents_on_fingerprint_change(monkeypatch):
    fake = FakeChroma()
    old_custom = fake.get_or_create_collection(
        "odysseus_memories_custom",
        metadata={
            "embedding_lane": "custom",
            "embedding_dimension": 384,
            "embedding_fingerprint": "old",
        },
    )
    old_custom.add(
        ids=["existing-memory"],
        embeddings=[[0.0] * 384],
        documents=["existing custom memory"],
        metadatas=[{"source": "memory"}],
    )
    patch_chroma(monkeypatch, fake)

    import src.embedding_lanes as lanes

    monkeypatch.setattr(lanes, "_build_custom_client", lambda: FakeEmbedder(768, "nomic", "http://embeddings/v1"))

    def fail_fastembed():
        raise RuntimeError("fastembed missing")

    monkeypatch.setattr(lanes, "_build_fastembed_client", fail_fastembed)

    built = build_embedding_lanes("odysseus_memories")

    assert [lane.name for lane in built] == [LANE_CUSTOM]
    assert "odysseus_memories_custom" in fake.deleted
    rebuilt = fake.collections["odysseus_memories_custom"]
    assert rebuilt.count() == 1
    assert rebuilt.get()["ids"] == ["existing-memory"]
    assert len(rebuilt.rows["existing-memory"]["embedding"]) == 768


def test_lane_reset_keeps_existing_collection_when_reembed_fails(monkeypatch):
    fake = FakeChroma()
    old_custom = fake.get_or_create_collection(
        "odysseus_memories_custom",
        metadata={
            "embedding_lane": "custom",
            "embedding_dimension": 384,
            "embedding_fingerprint": "old",
        },
    )
    old_custom.add(
        ids=["existing-memory"],
        embeddings=[[0.0] * 384],
        documents=["existing custom memory"],
        metadatas=[{"source": "memory"}],
    )
    patch_chroma(monkeypatch, fake)

    import src.embedding_lanes as lanes

    monkeypatch.setattr(lanes, "_build_custom_client", lambda: FailingEmbedder(768, "nomic", "http://embeddings/v1"))
    monkeypatch.setattr(lanes, "_build_fastembed_client", lambda: FakeEmbedder(384, "mini", "local://fastembed"))

    built = build_embedding_lanes("odysseus_memories")

    assert [lane.name for lane in built] == [LANE_FASTEMBED]
    assert "odysseus_memories_custom" not in fake.deleted
    assert fake.collections["odysseus_memories_custom"].count() == 1
    assert len(fake.collections["odysseus_memories_custom"].rows["existing-memory"]["embedding"]) == 384


def test_lane_reset_keeps_existing_collection_when_preserve_read_fails(monkeypatch):
    fake = FakeChroma()
    old_custom = fake.get_or_create_collection(
        "odysseus_memories_custom",
        metadata={
            "embedding_lane": "custom",
            "embedding_dimension": 384,
            "embedding_fingerprint": "old",
        },
    )
    old_custom.add(
        ids=["existing-memory"],
        embeddings=[[0.0] * 384],
        documents=["existing custom memory"],
        metadatas=[{"source": "memory"}],
    )

    def fail_get(*_args, **_kwargs):
        raise RuntimeError("chroma read failed")

    old_custom.get = fail_get
    patch_chroma(monkeypatch, fake)

    import src.embedding_lanes as lanes

    monkeypatch.setattr(lanes, "_build_custom_client", lambda: FakeEmbedder(768, "nomic", "http://embeddings/v1"))

    def fail_fastembed():
        raise RuntimeError("fastembed missing")

    monkeypatch.setattr(lanes, "_build_fastembed_client", fail_fastembed)

    built = build_embedding_lanes("odysseus_memories")

    assert built == []
    assert "odysseus_memories_custom" not in fake.deleted
    assert "odysseus_memories_custom" in fake.collections


def test_lane_reset_restores_existing_collection_when_rewrite_fails(monkeypatch):
    fake = FakeChroma()
    old_custom = fake.get_or_create_collection(
        "odysseus_memories_custom",
        metadata={
            "embedding_lane": "custom",
            "embedding_dimension": 384,
            "embedding_fingerprint": "old",
        },
    )
    old_custom.add(
        ids=["existing-memory"],
        embeddings=[[0.0] * 384],
        documents=["existing custom memory"],
        metadatas=[{"source": "memory"}],
    )
    fake.fail_next_add_for["odysseus_memories_custom"] = 1
    patch_chroma(monkeypatch, fake)

    import src.embedding_lanes as lanes

    monkeypatch.setattr(lanes, "_build_custom_client", lambda: FakeEmbedder(768, "nomic", "http://embeddings/v1"))

    def fail_fastembed():
        raise RuntimeError("fastembed missing")

    monkeypatch.setattr(lanes, "_build_fastembed_client", fail_fastembed)

    built = build_embedding_lanes("odysseus_memories")

    assert built == []
    restored = fake.collections["odysseus_memories_custom"]
    assert restored.count() == 1
    assert restored.get()["ids"] == ["existing-memory"]
    assert len(restored.rows["existing-memory"]["embedding"]) == 384


def test_build_embedding_lanes_uses_fastembed_when_custom_unavailable(monkeypatch):
    fake = FakeChroma()
    patch_chroma(monkeypatch, fake)

    import src.embedding_lanes as lanes

    def fail_custom():
        raise RuntimeError("down")

    monkeypatch.setattr(lanes, "_build_custom_client", fail_custom)
    monkeypatch.setattr(lanes, "_build_fastembed_client", lambda: FakeEmbedder(384, "mini", "local://fastembed"))

    built = build_embedding_lanes("odysseus_tool_index")

    assert [lane.name for lane in built] == [LANE_FASTEMBED]
    assert built[0].collection_name == "odysseus_tool_index_fastembed"


def test_custom_lane_preserves_default_embedding_client_probe(monkeypatch):
    import src.embedding_lanes as lanes
    import src.embeddings as embeddings

    embeddings.reset_http_embed_state()
    monkeypatch.setattr(lanes, "_load_custom_endpoint", lambda: {})

    calls = []

    class DefaultClient(FakeEmbedder):
        def __init__(self, url=None, model=None, api_key=None):
            calls.append({"url": url, "model": model, "api_key": api_key})
            super().__init__(768, model or "all-minilm:l6-v2", url or "http://localhost:11434/v1/embeddings")

    monkeypatch.setattr(embeddings, "EmbeddingClient", DefaultClient)

    client = lanes._build_custom_client()

    assert calls == [{"url": None, "model": None, "api_key": None}]
    assert client.url == "http://localhost:11434/v1/embeddings"
    embeddings.reset_http_embed_state()


def test_custom_lane_uses_http_down_latch(monkeypatch):
    import src.embedding_lanes as lanes
    import src.embeddings as embeddings

    embeddings.reset_http_embed_state()
    calls = []

    class DownClient:
        def __init__(self, url=None, model=None, api_key=None):
            calls.append({"url": url, "model": model, "api_key": api_key})

        def get_sentence_embedding_dimension(self):
            raise RuntimeError("endpoint down")

    class LocalFastEmbed(FakeEmbedder):
        def __init__(self):
            super().__init__(384, "mini", "local://fastembed")

    monkeypatch.setattr(embeddings, "EmbeddingClient", DownClient)
    monkeypatch.setattr(embeddings, "FastEmbedClient", LocalFastEmbed)

    with pytest.raises(RuntimeError, match="HTTP embedding lane unavailable"):
        lanes._build_custom_client()
    with pytest.raises(RuntimeError, match="HTTP embedding lane unavailable"):
        lanes._build_custom_client()

    assert calls == [{"url": None, "model": None, "api_key": None}]
    embeddings.reset_http_embed_state()


# ── the FastEmbed lane off-switch ───────────────────────────────────────────
#
# ChromaDB is the vector store; FastEmbed is one of the embedders that fills
# it. They are layers, not alternatives, and until now the FastEmbed lane was
# unconditional -- "we run a real embedding model, stop maintaining a second
# 384-dimension MiniLM index beside it" was not a thing that could be said.

def _both_clients(monkeypatch):
    import src.embedding_lanes as lanes

    monkeypatch.setattr(
        lanes, "_build_custom_client",
        lambda: FakeEmbedder(768, "nomic-embed-text", "http://embeddings/v1"),
    )
    monkeypatch.setattr(
        lanes, "_build_fastembed_client",
        lambda: FakeEmbedder(384, "sentence-transformers/all-MiniLM-L6-v2", "local://fastembed"),
    )


def test_fastembed_lane_defaults_to_on(monkeypatch):
    patch_chroma(monkeypatch, FakeChroma())
    _both_clients(monkeypatch)
    monkeypatch.delenv("ODYSSEUS_FASTEMBED_LANE", raising=False)

    assert [l.name for l in build_embedding_lanes("odysseus_memories")] == [
        LANE_CUSTOM, LANE_FASTEMBED
    ]


def test_fastembed_lane_off_skips_it_when_the_custom_lane_is_up(monkeypatch):
    patch_chroma(monkeypatch, FakeChroma())
    _both_clients(monkeypatch)
    monkeypatch.setenv("ODYSSEUS_FASTEMBED_LANE", "off")

    assert [l.name for l in build_embedding_lanes("odysseus_memories")] == [LANE_CUSTOM]


def test_fastembed_lane_off_still_falls_back_when_the_endpoint_is_down(monkeypatch):
    """`off` must degrade retrieval, never delete it: with no custom lane the
    fallback is built anyway rather than leaving the store with no lanes."""
    import src.embedding_lanes as lanes

    patch_chroma(monkeypatch, FakeChroma())
    _both_clients(monkeypatch)
    monkeypatch.setattr(
        lanes, "_build_custom_client",
        lambda: (_ for _ in ()).throw(RuntimeError("HTTP embedding lane unavailable")),
    )
    monkeypatch.setenv("ODYSSEUS_FASTEMBED_LANE", "off")

    assert [l.name for l in build_embedding_lanes("odysseus_memories")] == [LANE_FASTEMBED]


def test_primary_collection_pairs_with_the_lane_the_store_embeds_through(monkeypatch):
    """Stores keep one `_collection` and one embedder. The embedder was
    lanes[0] (custom when configured) while the collection preferred the
    FastEmbed lane -- different models, different dimensions."""
    from src.embedding_lanes import primary_collection

    patch_chroma(monkeypatch, FakeChroma())
    _both_clients(monkeypatch)
    monkeypatch.delenv("ODYSSEUS_FASTEMBED_LANE", raising=False)

    built = build_embedding_lanes("odysseus_memories")
    assert primary_collection(built) is built[0].collection
    assert primary_collection([]) is None


def test_missing_legacy_collection_is_cached_without_hiding_other_chroma_errors(monkeypatch):
    import src.embedding_lanes as lanes

    class MissingLegacyChroma(FakeChroma):
        def __init__(self):
            super().__init__()
            self.legacy_reads = 0

        def get_collection(self, name):
            if name == "legacy":
                self.legacy_reads += 1
                raise RuntimeError("404 collection not found")
            return super().get_collection(name)

    fake = MissingLegacyChroma()
    monkeypatch.setattr("src.chroma_client.get_chroma_client", lambda: fake)
    lanes._legacy_missing_until.clear()
    lane = type("Lane", (), {"collection": fake.get_or_create_collection("legacy_fastembed")})()

    lanes.migrate_legacy_collection("legacy", [lane])
    lanes.migrate_legacy_collection("legacy", [lane])

    assert fake.legacy_reads == 1


def test_legacy_migration_does_not_cache_a_transport_failure(monkeypatch):
    import src.embedding_lanes as lanes

    class UnavailableChroma(FakeChroma):
        def __init__(self):
            super().__init__()
            self.legacy_reads = 0

        def get_collection(self, name):
            self.legacy_reads += 1
            raise RuntimeError("connection refused")

    fake = UnavailableChroma()
    monkeypatch.setattr("src.chroma_client.get_chroma_client", lambda: fake)
    lanes._legacy_missing_until.clear()
    lane = type("Lane", (), {"collection": fake.get_or_create_collection("legacy_fastembed")})()

    lanes.migrate_legacy_collection("legacy", [lane])
    lanes.migrate_legacy_collection("legacy", [lane])

    assert fake.legacy_reads == 2
