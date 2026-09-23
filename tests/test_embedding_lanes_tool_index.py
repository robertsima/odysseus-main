import pytest

from src.embedding_lanes import (
    EmbeddingLane,
    LANE_CUSTOM,
    LANE_FASTEMBED,
)
from tests.helpers.embedding_lanes import (
    FakeChroma,
    FakeCollection,
    FakeEmbedder,
    FailingEmbedder,
    patch_chroma,
)


def test_tool_index_indexes_and_retrieves_from_available_lanes(monkeypatch):
    fake = FakeChroma()
    patch_chroma(monkeypatch, fake)

    import src.embedding_lanes as lanes

    monkeypatch.setattr(lanes, "_build_fastembed_client", lambda: FakeEmbedder(384, "mini", "local://fastembed"))

    from src.tool_index import ToolIndex

    index = ToolIndex()
    index.index_builtin_tools()

    assert fake.collections["odysseus_tool_index_fastembed"].count() > 0
    assert "bash" in index.retrieve("run a shell command", k=10)


def test_tool_index_builtin_indexing_fails_when_all_lanes_fail():
    custom_lane = EmbeddingLane(
        name=LANE_CUSTOM,
        client=FailingEmbedder(768, "nomic", "http://embeddings/v1"),
        collection=FakeCollection("odysseus_tool_index_custom", metadata={"embedding_lane": "custom"}),
        collection_name="odysseus_tool_index_custom",
        model="nomic",
        url="http://embeddings/v1",
        dimension=768,
        fingerprint="custom",
    )
    fast_lane = EmbeddingLane(
        name=LANE_FASTEMBED,
        client=FailingEmbedder(384, "mini", "local://fastembed"),
        collection=FakeCollection("odysseus_tool_index_fastembed", metadata={"embedding_lane": "fastembed"}),
        collection_name="odysseus_tool_index_fastembed",
        model="mini",
        url="local://fastembed",
        dimension=384,
        fingerprint="fast",
    )

    from src.tool_index import ToolIndex

    index = ToolIndex.__new__(ToolIndex)
    index._lanes = [custom_lane, fast_lane]
    index._healthy = True

    with pytest.raises(RuntimeError, match="all embedding lanes"):
        index.index_builtin_tools()
    assert not index.healthy


def test_tool_index_retrieval_continues_when_custom_lane_query_fails():
    custom_collection = FakeCollection("odysseus_tool_index_custom", metadata={"embedding_lane": "custom"})
    fast_collection = FakeCollection("odysseus_tool_index_fastembed", metadata={"embedding_lane": "fastembed"})
    fast_collection.add(
        ids=["builtin_bash"],
        embeddings=[[0.0] * 384],
        documents=["Tool: bash\nRun shell commands"],
        metadatas=[{"tool_name": "bash", "tool_type": "builtin"}],
    )

    def fail_query(*_args, **_kwargs):
        raise RuntimeError("custom endpoint down")

    custom_collection.add(
        ids=["builtin_python"],
        embeddings=[[0.0] * 768],
        documents=["Tool: python\nRun Python"],
        metadatas=[{"tool_name": "python", "tool_type": "builtin"}],
    )
    custom_collection.query = fail_query

    custom_lane = EmbeddingLane(
        name=LANE_CUSTOM,
        client=FakeEmbedder(768, "nomic", "http://embeddings/v1"),
        collection=custom_collection,
        collection_name="odysseus_tool_index_custom",
        model="nomic",
        url="http://embeddings/v1",
        dimension=768,
        fingerprint="custom",
    )
    fast_lane = EmbeddingLane(
        name=LANE_FASTEMBED,
        client=FakeEmbedder(384, "mini", "local://fastembed"),
        collection=fast_collection,
        collection_name="odysseus_tool_index_fastembed",
        model="mini",
        url="local://fastembed",
        dimension=384,
        fingerprint="fast",
    )

    from src.tool_index import ToolIndex

    index = ToolIndex.__new__(ToolIndex)
    index._lanes = [custom_lane, fast_lane]

    assert index.retrieve("run shell", k=5) == ["bash"]


def test_tool_index_merges_fallback_tool_results_before_limit():
    custom_collection = FakeCollection("odysseus_tool_index_custom", metadata={"embedding_lane": "custom"})
    fast_collection = FakeCollection("odysseus_tool_index_fastembed", metadata={"embedding_lane": "fastembed"})
    custom_collection.add(
        ids=["builtin_one", "builtin_two"],
        embeddings=[[0.0] * 768, [0.0] * 768],
        documents=["Tool: one", "Tool: two"],
        metadatas=[
            {"tool_name": "one", "tool_type": "builtin"},
            {"tool_name": "two", "tool_type": "builtin"},
        ],
    )
    fast_collection.add(
        ids=["mcp_current"],
        embeddings=[[0.0] * 384],
        documents=["Tool: current MCP"],
        metadatas=[{"tool_name": "current_mcp", "tool_type": "mcp"}],
    )

    custom_collection.query = lambda **_kwargs: {
        "ids": [["builtin_one", "builtin_two"]],
        "metadatas": [[
            {"tool_name": "one", "tool_type": "builtin"},
            {"tool_name": "two", "tool_type": "builtin"},
        ]],
        "distances": [[0.20, 0.21]],
    }
    fast_collection.query = lambda **_kwargs: {
        "ids": [["mcp_current"]],
        "metadatas": [[{"tool_name": "current_mcp", "tool_type": "mcp"}]],
        "distances": [[0.05]],
    }

    custom_lane = EmbeddingLane(
        name=LANE_CUSTOM,
        client=FakeEmbedder(768, "nomic", "http://embeddings/v1"),
        collection=custom_collection,
        collection_name="odysseus_tool_index_custom",
        model="nomic",
        url="http://embeddings/v1",
        dimension=768,
        fingerprint="custom",
    )
    fast_lane = EmbeddingLane(
        name=LANE_FASTEMBED,
        client=FakeEmbedder(384, "mini", "local://fastembed"),
        collection=fast_collection,
        collection_name="odysseus_tool_index_fastembed",
        model="mini",
        url="local://fastembed",
        dimension=384,
        fingerprint="fast",
    )

    from src.tool_index import ToolIndex

    index = ToolIndex.__new__(ToolIndex)
    index._lanes = [custom_lane, fast_lane]

    assert index.retrieve("current mcp", k=2) == ["current_mcp", "one"]


def test_tool_index_rejects_weak_email_neighbour_for_ntfy_request():
    """Top-K alone is not intent: a notification tool must not pull email
    schemas in when email is only a low-similarity neighbour."""
    collection = FakeCollection("odysseus_tool_index_custom", metadata={"embedding_lane": "custom"})
    collection.add(
        ids=["mcp_ntfy", "builtin_email"],
        embeddings=[[0.0] * 768, [0.0] * 768],
        documents=["Tool: ntfy", "Tool: list_emails"],
        metadatas=[
            {"tool_name": "mcp__ntfy__send", "tool_type": "mcp"},
            {"tool_name": "list_emails", "tool_type": "builtin"},
        ],
    )
    collection.query = lambda **_kwargs: {
        "ids": [["mcp_ntfy", "builtin_email"]],
        "metadatas": [[
            {"tool_name": "mcp__ntfy__send", "tool_type": "mcp"},
            {"tool_name": "list_emails", "tool_type": "builtin"},
        ]],
        # Scores are 0.80 and 0.17; the latter is below the calibrated floor.
        "distances": [[0.20, 0.83]],
    }
    lane = EmbeddingLane(
        name=LANE_CUSTOM,
        client=FakeEmbedder(768, "nomic", "http://embeddings/v1"),
        collection=collection,
        collection_name="odysseus_tool_index_custom",
        model="nomic", url="http://embeddings/v1", dimension=768, fingerprint="custom",
    )
    from src.tool_index import ToolIndex

    index = ToolIndex.__new__(ToolIndex)
    index._lanes = [lane]
    assert index.retrieve("publish an ntfy notification", k=2) == ["mcp__ntfy__send"]
    selected = index.get_tools_for_query("publish an ntfy notification", k=2)
    from src.tool_index import ALWAYS_AVAILABLE
    assert set(ALWAYS_AVAILABLE) <= selected
    assert "mcp__ntfy__send" in selected
    assert "list_emails" not in selected
