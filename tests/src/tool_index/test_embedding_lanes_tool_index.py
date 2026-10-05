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


def _single_lane_index(rows):
    """A one-lane index whose query returns ``rows`` = [(name, type, score, document)]."""
    collection = FakeCollection("odysseus_tool_index_custom", metadata={"embedding_lane": "custom"})
    collection.add(
        ids=[f"row_{i}" for i in range(len(rows))],
        embeddings=[[0.0] * 768 for _ in rows],
        documents=[doc for *_rest, doc in rows],
        metadatas=[{"tool_name": name, "tool_type": kind} for name, kind, _s, _d in rows],
    )
    collection.query = lambda **_kwargs: {
        "ids": [[f"row_{i}" for i in range(len(rows))]],
        "metadatas": [[{"tool_name": name, "tool_type": kind} for name, kind, _s, _d in rows]],
        "documents": [[doc for *_rest, doc in rows]],
        "distances": [[round(1.0 - score, 4) for _n, _k, score, _d in rows]],
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
    return index


def _penpot(tool, description):
    return (f"mcp__c5ec6d7a__{tool}", "mcp", None,
            f"Tool: mcp__c5ec6d7a__{tool} (server: Penpot (robert@example.com))\n[MCP:Penpot] {description}")


def _with_score(row, score):
    name, kind, _old, doc = row
    return (name, kind, score, doc)


def test_a_verb_alone_does_not_retrieve_a_large_servers_tool():
    """2026-09-26: "Can you upgrade with shell" retrieved Penpot's update_webhook
    next to bash, and the follow-up retrieved update_shape. Scores are the ones
    fastembed gives these texts: the Penpot tools sit just under bash and share
    nothing with the request but a verb close to "update"."""
    index = _single_lane_index([
        ("bash", "builtin", 0.294, "Tool: bash\nRun shell commands"),
        _with_score(_penpot("update_webhook", "Update an existing webhook"), 0.250),
        _with_score(_penpot("update_shape", "Update shape properties"), 0.228),
        ("write_file", "builtin", 0.202, "Tool: write_file\nWrite a file"),
    ])
    for query in ("Can you upgrade with shell", "ok use shell to upgrade\nCan you upgrade with shell"):
        assert index.retrieve(query, k=8) == ["bash", "write_file"], query


def test_mcp_tools_the_request_is_about_are_still_retrieved():
    # Best fit overall: the embedding alone is enough.
    index = _single_lane_index([
        _with_score(_penpot("create_rectangle", "Create a rectangle shape"), 0.383),
        ("generate_image", "builtin", 0.186, "Tool: generate_image\nGenerate an image"),
    ])
    assert index.retrieve("draw a blue box on my design canvas", k=8) == [
        "mcp__c5ec6d7a__create_rectangle", "generate_image"]
    # Below the best built-in, but the request names the tool's subject...
    index = _single_lane_index([
        _with_score(_penpot("update_webhook", "Update an existing webhook"), 0.726),
        ("manage_webhooks", "builtin", 0.615, "Tool: manage_webhooks\nWebhook management"),
        _with_score(_penpot("create_webhook", "Create a webhook"), 0.516),
        _with_score(_penpot("update_team", "Update team information"), 0.298),
    ])
    assert index.retrieve("update the webhook url", k=8) == [
        "mcp__c5ec6d7a__update_webhook", "manage_webhooks", "mcp__c5ec6d7a__create_webhook"]
    # ...or the server, by its label (the account in parentheses is not a name).
    index = _single_lane_index([
        ("edit_image", "builtin", 0.40, "Tool: edit_image\nEdit an image"),
        _with_score(_penpot("get_profile", "Get current user profile"), 0.30),
    ])
    assert index.retrieve("is penpot connected", k=8) == ["edit_image", "mcp__c5ec6d7a__get_profile"]
    assert index.retrieve("robert profile", k=8) == ["edit_image", "mcp__c5ec6d7a__get_profile"]
    assert index.retrieve("robert example com", k=8) == ["edit_image"]


def test_camel_case_mcp_names_anchor_on_their_words():
    index = _single_lane_index([
        ("ui_control", "builtin", 0.50, "Tool: ui_control\nToggle tools on/off"),
        ("mcp__ha__HassTurnOff", "mcp", 0.40, "Tool: mcp__ha__HassTurnOff (server: Home Assistant)\nTurn off"),
        ("mcp__ha__HassLightSet", "mcp", 0.35, "Tool: mcp__ha__HassLightSet (server: Home Assistant)\nSet light"),
    ])
    assert index.retrieve("turn off the kitchen lights", k=8) == [
        "ui_control", "mcp__ha__HassTurnOff", "mcp__ha__HassLightSet"]


def test_mcp_scores_are_compared_with_builtins_of_the_same_lane_only():
    """Two embedders score on different scales; a custom-lane built-in must not
    set the bar for a fastembed-lane MCP tool."""
    custom = FakeCollection("odysseus_tool_index_custom", metadata={"embedding_lane": "custom"})
    fast = FakeCollection("odysseus_tool_index_fastembed", metadata={"embedding_lane": "fastembed"})
    for coll in (custom, fast):
        coll.add(ids=["x"], embeddings=[[0.0] * (768 if coll is custom else 384)],
                 documents=["Tool: x"], metadatas=[{"tool_name": "x", "tool_type": "builtin"}])
    custom.query = lambda **_kwargs: {
        "metadatas": [[{"tool_name": "bash", "tool_type": "builtin"}]], "distances": [[0.10]]}
    fast.query = lambda **_kwargs: {
        "metadatas": [[{"tool_name": "mcp__penpot__update_shape", "tool_type": "mcp"}]],
        "distances": [[0.40]]}
    lanes = [
        EmbeddingLane(name=LANE_CUSTOM, client=FakeEmbedder(768, "nomic", "http://e/v1"), collection=custom,
                      collection_name="c", model="nomic", url="http://e/v1", dimension=768, fingerprint="c"),
        EmbeddingLane(name=LANE_FASTEMBED, client=FakeEmbedder(384, "mini", "local://fastembed"), collection=fast,
                      collection_name="f", model="mini", url="local://fastembed", dimension=384, fingerprint="f"),
    ]
    from src.tool_index import ToolIndex

    index = ToolIndex.__new__(ToolIndex)
    index._lanes = lanes
    assert index.retrieve("upgrade with shell", k=8) == ["bash", "mcp__penpot__update_shape"]
