"""The vault has to be reachable on an ordinary turn, not just a phrased one.

Reported symptom: "it does not seem like it's using vault as context
appropriately — it should be constantly using it". Four independent reasons,
each pinned here:

1. Indexing is one-shot and tracked in JSON, so an emptied vector store stays
   empty forever (the per-turn `count` with no `query` in the logs).
2. `rag_manager` is resolved once at import with a 2s ChromaDB probe; a slow
   ChromaDB froze `None` for the life of the process.
3. `search_documents` was reachable only through a literal phrase, and the
   prompt section telling the model to search the vault is emitted only when
   that tool is selected.
4. The chat UI defaulted document retrieval OFF for every fresh browser.
"""
import os

import pytest

pytestmark = pytest.mark.area_security


class _Rag:
    """Minimal stand-in for VectorRAG."""

    def __init__(self, count=0, healthy=True):
        self.healthy = healthy
        self._count = count
        self.indexed = []

    def get_stats(self):
        return {"document_count": self._count}

    def index_personal_documents(self, directory, owner=None, sensitivity=None):
        self.indexed.append((directory, sensitivity))
        return {"indexed_count": 7}


def _manager(tmp_path, rag, directories, sensitivity=None):
    from src.personal_docs import PersonalDocsManager

    mgr = PersonalDocsManager.__new__(PersonalDocsManager)
    mgr.personal_dir = str(tmp_path)
    mgr.rag_manager = rag
    mgr.indexed_directories = [str(d) for d in directories]
    mgr.directory_sensitivity = sensitivity or {}
    return mgr


def test_an_empty_index_with_tracked_directories_is_rebuilt(tmp_path):
    vault = tmp_path / "Vault Mind"
    vault.mkdir()
    rag = _Rag(count=0)
    mgr = _manager(tmp_path, rag, [vault], {str(vault): "public"})
    (tmp_path / ".vault_scan_state.json").write_text("{}", encoding="utf-8")

    result = mgr.reindex_if_empty()

    assert rag.indexed == [(str(vault), "public")]
    assert result["reindexed"] == [str(vault)]
    assert result["chunks"] == 7
    # The incremental scanner must re-walk, or it would skip what we rebuilt.
    assert not os.path.exists(tmp_path / ".vault_scan_state.json")


def test_a_populated_index_is_left_alone(tmp_path):
    vault = tmp_path / "Vault Mind"
    vault.mkdir()
    rag = _Rag(count=1423)
    state = tmp_path / ".vault_scan_state.json"
    state.write_text("{}", encoding="utf-8")

    result = _manager(tmp_path, rag, [vault]).reindex_if_empty()

    assert rag.indexed == []
    assert result["document_count"] == 1423
    assert state.exists()


def test_missing_directories_and_an_unhealthy_store_are_skipped(tmp_path):
    rag = _Rag(count=0)
    # Tracked but no longer on disk (unmounted volume): nothing to re-index.
    assert _manager(tmp_path, rag, [tmp_path / "gone"]).reindex_if_empty()["reindexed"] == []
    assert rag.indexed == []

    unhealthy = _Rag(count=0, healthy=False)
    vault = tmp_path / "Vault Mind"
    vault.mkdir()
    out = _manager(tmp_path, unhealthy, [vault]).reindex_if_empty()
    assert out["skipped"] == "vector store unavailable"
    assert unhealthy.indexed == []

    out = _manager(tmp_path, None, [vault]).reindex_if_empty()
    assert "skipped" in out


def test_one_directory_failing_does_not_stop_the_others(tmp_path):
    good, bad = tmp_path / "AI Mind", tmp_path / "Journal"
    good.mkdir()
    bad.mkdir()

    class _Flaky(_Rag):
        def index_personal_documents(self, directory, owner=None, sensitivity=None):
            if directory == str(bad):
                raise RuntimeError("embedding backend down")
            return super().index_personal_documents(directory, owner, sensitivity)

    rag = _Flaky(count=0)
    result = _manager(tmp_path, rag, [bad, good]).reindex_if_empty()
    assert result["reindexed"] == [str(good)]


def test_startup_runs_the_check():
    import inspect

    from src import app_initializer

    assert "reindex_if_empty()" in inspect.getsource(app_initializer)


def test_search_documents_is_always_available():
    """Reachable on any turn, like memory — not only after a literal phrase
    such as "my notes" or "obsidian"."""
    from src.tool_index import ALWAYS_AVAILABLE

    assert "search_documents" in ALWAYS_AVAILABLE


def test_low_signal_turns_still_offer_the_vault():
    from src.tool_index import ALWAYS_AVAILABLE, ToolIndex

    ti = ToolIndex.__new__(ToolIndex)
    ti.retrieve = lambda query, k=8: []
    assert "search_documents" in ti.get_tools_for_query("i like Umni", use_embeddings=False)
    assert ALWAYS_AVAILABLE <= ti.get_tools_for_query("anything", use_embeddings=False)


def test_chat_processor_resolves_a_missing_rag_manager_lazily():
    import inspect

    from src.chat_processor import ChatProcessor

    src = inspect.getsource(ChatProcessor)
    assert "rag_manager is None" in src
    assert "get_rag_manager()" in src


def test_the_ui_defaults_document_retrieval_on():
    """chat.js only sends use_rag when the box is UNchecked, so an off-by-
    default toggle meant the server's own default of True was never reached."""
    app_js = open("static/app.js", encoding="utf-8").read()
    assert "const ragState = st.rag || false;" not in app_js
    assert "st.rag === undefined || st.rag === null ? true : !!st.rag" in app_js
