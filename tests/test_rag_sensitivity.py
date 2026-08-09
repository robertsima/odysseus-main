"""Public/private document sensitivity: private content must never be retrieved
for, or listed to, a session served by a non-local endpoint.

The guarantee has to hold on every read path, because they fail independently:
  1. the Chroma ``where`` filter in VectorRAG.search,
  2. the Python-side scan in _keyword_search_fallback (used when every vector
     lane errors — a lane outage must not become a leak),
  3. the keyword index in personal_docs.retrieve_personal_keyword,
  4. the tool surface (filenames are disclosure on their own).

Unlabeled chunks stay public on purpose: they predate the feature and were
already reaching hosted models, so defaulting them to private would silently
empty an existing index. backfill_sensitivity stamps them so the public-only
filter can use a plain equality match.

Hermetic — no chromadb; VectorRAG runs against a fake collection.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import src.ai_interaction as ai
import src.chat_processor as chat_processor
import src.model_context as model_context
import src.personal_docs as personal_docs
import src.rag_vector as rag_vector
from src.rag_sensitivity import (
    SENSITIVITY_KEY,
    SENSITIVITY_PRIVATE,
    SENSITIVITY_PUBLIC,
    apply_sensitivity,
    metadata_is_private,
    normalize_sensitivity,
)


# --------------------------------------------------------------------------- #
# Label normalization
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["private", "PRIVATE", "  Private  "])
def test_only_explicit_private_is_private(value):
    assert normalize_sensitivity(value) == SENSITIVITY_PRIVATE


@pytest.mark.parametrize("value", [None, "", "public", "secret", "confidential", 3, ["private"]])
def test_everything_else_is_public(value):
    """A typo must not create a third label that no filter would ever match."""
    assert normalize_sensitivity(value) == SENSITIVITY_PUBLIC


def test_apply_sensitivity_does_not_mutate_caller_metadata():
    original = {"source": "/a/f.md"}
    stamped = apply_sensitivity(original, SENSITIVITY_PRIVATE)
    assert stamped[SENSITIVITY_KEY] == SENSITIVITY_PRIVATE
    assert SENSITIVITY_KEY not in original


def test_apply_sensitivity_keeps_existing_label_when_unspecified():
    stamped = apply_sensitivity({SENSITIVITY_KEY: SENSITIVITY_PRIVATE})
    assert stamped[SENSITIVITY_KEY] == SENSITIVITY_PRIVATE


def test_missing_label_is_not_private():
    assert metadata_is_private({"source": "/a/f.md"}) is False
    assert metadata_is_private(None) is False


# --------------------------------------------------------------------------- #
# Filter composition
# --------------------------------------------------------------------------- #


def test_where_combines_owner_and_public_only():
    assert rag_vector._build_where("alice", allow_private=False) == {
        "$and": [{"owner": "alice"}, {SENSITIVITY_KEY: SENSITIVITY_PUBLIC}]
    }


def test_where_public_only_without_owner():
    assert rag_vector._build_where(None, allow_private=False) == {
        SENSITIVITY_KEY: SENSITIVITY_PUBLIC
    }


def test_where_unchanged_when_private_allowed():
    assert rag_vector._build_where("alice", allow_private=True) == {"owner": "alice"}
    assert rag_vector._build_where(None, allow_private=True) is None


# --------------------------------------------------------------------------- #
# VectorRAG
# --------------------------------------------------------------------------- #


class _FakeCollection:
    def __init__(self, rows):
        self._ids = [r[0] for r in rows]
        self._metas = [r[1] for r in rows]
        self._docs = [r[2] if len(r) > 2 else "" for r in rows]

    def count(self):
        return len(self._ids)

    def get(self, include=None, ids=None):
        if ids is None:
            keep = range(len(self._ids))
        else:
            wanted = set(ids)
            keep = [i for i, doc_id in enumerate(self._ids) if doc_id in wanted]
        return {
            "ids": [self._ids[i] for i in keep],
            "metadatas": [self._metas[i] for i in keep],
            "documents": [self._docs[i] for i in keep],
        }

    def add(self, ids=None, embeddings=None, documents=None, metadatas=None):
        for doc_id, doc, meta in zip(ids or [], documents or [], metadatas or []):
            self._ids.append(doc_id)
            self._docs.append(doc)
            self._metas.append(meta)

    def update(self, ids=None, metadatas=None):
        for doc_id, meta in zip(ids or [], metadatas or []):
            self._metas[self._ids.index(doc_id)] = meta


class _FakeLane:
    """Minimal EmbeddingLane stand-in sharing one fake collection."""

    name = "fake"

    def __init__(self, collection):
        self.collection = collection

    def encode(self, texts):
        return [[0.0] for _ in texts]

    def count(self):
        return self.collection.count()


def _make_lane_rag(rows=()):
    rag = rag_vector.VectorRAG.__new__(rag_vector.VectorRAG)
    collection = _FakeCollection(list(rows))
    rag._collection = collection
    rag._lanes = [_FakeLane(collection)]
    rag._healthy = True
    return rag, collection


def _make_vectorrag(rows):
    rag = rag_vector.VectorRAG.__new__(rag_vector.VectorRAG)  # skip Chroma connect
    rag._collection = _FakeCollection(rows)
    rag._healthy = True
    return rag


def test_search_sends_public_only_filter(monkeypatch):
    """The composed filter must actually reach the lane query."""
    captured = {}

    def _fake_query_lanes(lanes, query, n_results, include, where=None, raise_if_all_failed=False):
        captured["where"] = where
        return []

    rag = _make_vectorrag([])
    rag._lanes = ["lane"]
    monkeypatch.setattr(rag_vector, "query_lanes", _fake_query_lanes)
    monkeypatch.setattr(rag_vector, "lane_count", lambda lanes: 1)

    rag.search("notes", k=5, owner="alice", allow_private=False)

    assert captured["where"] == {
        "$and": [{"owner": "alice"}, {SENSITIVITY_KEY: SENSITIVITY_PUBLIC}]
    }


def test_keyword_fallback_drops_private():
    """A vector-lane outage must not degrade into a private-content leak."""
    rows = [
        ("pub", {"source": "/a/pub.md", SENSITIVITY_KEY: SENSITIVITY_PUBLIC}, "vault budget notes"),
        ("priv", {"source": "/a/priv.md", SENSITIVITY_KEY: SENSITIVITY_PRIVATE}, "vault budget notes"),
        ("legacy", {"source": "/a/legacy.md"}, "vault budget notes"),  # unlabeled
    ]
    rag = _make_vectorrag(rows)

    public_only = rag._keyword_search_fallback("budget", k=10, allow_private=False)
    assert {r["id"] for r in public_only} == {"pub", "legacy"}

    everything = rag._keyword_search_fallback("budget", k=10, allow_private=True)
    assert {r["id"] for r in everything} == {"pub", "priv", "legacy"}


def test_keyword_fallback_applies_owner_and_sensitivity_together():
    rows = [
        ("mine-pub", {"source": "/a/1.md", "owner": "alice", SENSITIVITY_KEY: SENSITIVITY_PUBLIC}, "budget"),
        ("mine-priv", {"source": "/a/2.md", "owner": "alice", SENSITIVITY_KEY: SENSITIVITY_PRIVATE}, "budget"),
        ("theirs-pub", {"source": "/a/3.md", "owner": "bob", SENSITIVITY_KEY: SENSITIVITY_PUBLIC}, "budget"),
    ]
    rag = _make_vectorrag(rows)
    out = rag._keyword_search_fallback("budget", k=10, owner="alice", allow_private=False)
    assert {r["id"] for r in out} == {"mine-pub"}


def test_backfill_labels_only_unlabeled_chunks():
    rows = [
        ("legacy", {"source": "/a/1.md"}, ""),
        ("private", {"source": "/a/2.md", SENSITIVITY_KEY: SENSITIVITY_PRIVATE}, ""),
    ]
    rag = _make_vectorrag(rows)

    result = rag.backfill_sensitivity()

    assert result["updated_count"] == 1
    metas = dict(zip(rag._collection.get()["ids"], rag._collection.get()["metadatas"]))
    assert metas["legacy"][SENSITIVITY_KEY] == SENSITIVITY_PUBLIC
    assert metas["private"][SENSITIVITY_KEY] == SENSITIVITY_PRIVATE, "must not clobber a real label"


def test_backfill_is_idempotent():
    rag = _make_vectorrag([("legacy", {"source": "/a/1.md"}, "")])
    assert rag.backfill_sensitivity()["updated_count"] == 1
    assert rag.backfill_sensitivity()["updated_count"] == 0


def test_add_document_always_stamps_a_label(monkeypatch):
    """Uploads and attachment capture build metadata by hand; the store — not
    each call site — is what guarantees the key is present."""
    written = {}

    class _Lane:
        name = "fake"

        class collection:
            @staticmethod
            def get(ids=None):
                return {"ids": []}

            @staticmethod
            def add(ids=None, embeddings=None, documents=None, metadatas=None):
                written["meta"] = metadatas[0]

        @staticmethod
        def encode(texts):
            return [[0.0]]

    rag = _make_vectorrag([])
    rag._lanes = [_Lane()]

    assert rag.add_document("hello", {"source": "/a/f.md"}) is True
    assert written["meta"][SENSITIVITY_KEY] == SENSITIVITY_PUBLIC


def test_reindexing_same_content_as_private_wins():
    """The mounted-vault case: index the whole vault public, then its Private/
    subfolder private. Chunk ids are content-derived, so the second pass hits
    the duplicate branch — the private label must still take effect."""
    rag, collection = _make_lane_rag()

    text = "the thing I do not want sent to an API"
    assert rag.add_document(text, {"source": "/vault/Private/note.md"}) is True
    stored = collection.get()["metadatas"][0]
    assert stored[SENSITIVITY_KEY] == SENSITIVITY_PUBLIC

    # Same content, now reached via the explicitly private subfolder.
    assert rag.add_document(
        text, {"source": "/vault/Private/note.md", SENSITIVITY_KEY: SENSITIVITY_PRIVATE}
    ) is True

    assert len(collection.get()["ids"]) == 1, "must not duplicate the chunk"
    assert collection.get()["metadatas"][0][SENSITIVITY_KEY] == SENSITIVITY_PRIVATE


def test_public_reindex_never_downgrades_private():
    """The inverse must not hold, or re-running a public index would undo it."""
    rag, collection = _make_lane_rag()

    text = "sensitive"
    rag.add_document(text, {"source": "/vault/Private/n.md", SENSITIVITY_KEY: SENSITIVITY_PRIVATE})
    rag.add_document(text, {"source": "/vault/n.md", SENSITIVITY_KEY: SENSITIVITY_PUBLIC})

    assert collection.get()["metadatas"][0][SENSITIVITY_KEY] == SENSITIVITY_PRIVATE


def test_promotion_preserves_other_metadata():
    rag, collection = _make_lane_rag()
    text = "note body"
    rag.add_document(text, {"source": "/vault/n.md", "owner": "alice", "filename": "n.md"})
    rag.add_document(text, {"source": "/vault/n.md", "owner": "alice", SENSITIVITY_KEY: SENSITIVITY_PRIVATE})

    meta = collection.get()["metadatas"][0]
    assert meta[SENSITIVITY_KEY] == SENSITIVITY_PRIVATE
    assert meta["owner"] == "alice"
    assert meta["filename"] == "n.md", "provenance must survive the relabel"


def test_batch_duplicate_also_promotes_to_private():
    rag, collection = _make_lane_rag()
    text = "batch body"

    rag.add_documents_batch([(text, {"source": "/vault/n.md"})])
    assert collection.get()["metadatas"][0][SENSITIVITY_KEY] == SENSITIVITY_PUBLIC

    rag.add_documents_batch(
        [(text, {"source": "/vault/Private/n.md", SENSITIVITY_KEY: SENSITIVITY_PRIVATE})]
    )

    assert len(collection.get()["ids"]) == 1
    assert collection.get()["metadatas"][0][SENSITIVITY_KEY] == SENSITIVITY_PRIVATE


def test_nested_private_survives_a_full_reindex(tmp_path):
    """index_all_directories walks the base tree and each tracked subtree; the
    private subfolder must come out private whichever pass wrote the chunk."""
    personal = tmp_path / "personal"
    vault = personal / "vault"
    private = vault / "Private"
    private.mkdir(parents=True)
    (vault / "public.md").write_text("public note", encoding="utf-8")
    (private / "secret.md").write_text("secret note", encoding="utf-8")

    calls = []

    class _Rag:
        def index_personal_documents(self, directory, owner=None, sensitivity=None):
            calls.append((os.path.abspath(directory), sensitivity))
            return {"success": True, "indexed_count": 1}

    mgr = personal_docs.PersonalDocsManager(str(personal), rag_manager=_Rag())
    mgr.add_directory(str(vault), index=False, sensitivity="public")
    mgr.add_directory(str(private), index=False, sensitivity="private")
    calls.clear()

    mgr.index_all_directories()

    by_dir = dict(calls)
    assert by_dir[os.path.abspath(str(private))] == SENSITIVITY_PRIVATE
    assert by_dir[os.path.abspath(str(vault))] == SENSITIVITY_PUBLIC
    assert by_dir[os.path.abspath(str(personal))] == SENSITIVITY_PUBLIC


def test_set_directory_sensitivity_is_path_bounded():
    """Relabelling <root>/docs must not catch <root>/docs2 — same boundary rule
    as remove_directory."""
    root = os.path.abspath(os.sep + "a")
    docs = os.path.join(root, "docs")
    rows = [
        ("a", {"source": os.path.join(docs, "f1.md"), SENSITIVITY_KEY: SENSITIVITY_PUBLIC}, ""),
        ("b", {"source": os.path.join(docs, "sub", "f2.md"), SENSITIVITY_KEY: SENSITIVITY_PUBLIC}, ""),
        ("c", {"source": os.path.join(root, "docs2", "f3.md"), SENSITIVITY_KEY: SENSITIVITY_PUBLIC}, ""),
        ("d", {"filename": "no-source.md"}, ""),
    ]
    rag = _make_vectorrag(rows)

    res = rag.set_directory_sensitivity(docs, SENSITIVITY_PRIVATE)

    metas = dict(zip(rag._collection.get()["ids"], rag._collection.get()["metadatas"]))
    assert res["updated_count"] == 2
    assert metas["a"][SENSITIVITY_KEY] == SENSITIVITY_PRIVATE
    assert metas["b"][SENSITIVITY_KEY] == SENSITIVITY_PRIVATE
    assert metas["c"][SENSITIVITY_KEY] == SENSITIVITY_PUBLIC, "sibling prefix must be untouched"
    assert SENSITIVITY_KEY not in metas["d"]


# --------------------------------------------------------------------------- #
# Keyword index (personal_docs)
# --------------------------------------------------------------------------- #


def test_retrieve_personal_keyword_skips_private():
    index = [
        {"name": "pub.md", "sensitivity": SENSITIVITY_PUBLIC, "chunks": ["quarterly budget"]},
        {"name": "priv.md", "sensitivity": SENSITIVITY_PRIVATE, "chunks": ["quarterly budget"]},
        {"name": "legacy.md", "chunks": ["quarterly budget"]},
    ]
    out = personal_docs.retrieve_personal_keyword(index, "budget", k=10, allow_private=False)
    joined = "\n".join(out)
    assert "pub.md" in joined
    assert "legacy.md" in joined
    assert "priv.md" not in joined


# --------------------------------------------------------------------------- #
# PersonalDocsManager
# --------------------------------------------------------------------------- #


class _FakeRag:
    def __init__(self):
        self.indexed = []
        self.relabelled = []

    def index_personal_documents(self, directory, owner=None, sensitivity=None):
        self.indexed.append((directory, owner, sensitivity))
        return {"success": True, "indexed_count": 1}

    def set_directory_sensitivity(self, directory, sensitivity):
        self.relabelled.append((directory, sensitivity))
        return {"success": True, "updated_count": 3}

    def remove_directory(self, directory):
        return {"success": True, "removed_count": 0}


def _manager(tmp_path, rag=None):
    personal = tmp_path / "personal"
    personal.mkdir(exist_ok=True)
    return personal_docs.PersonalDocsManager(str(personal), rag_manager=rag)


def test_add_directory_records_and_forwards_label(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("secret plans", encoding="utf-8")

    rag = _FakeRag()
    mgr = _manager(tmp_path, rag)
    mgr.add_directory(str(vault), sensitivity="private")

    assert rag.indexed == [(os.path.abspath(str(vault)), None, SENSITIVITY_PRIVATE)]
    assert mgr.directory_sensitivity[os.path.abspath(str(vault))] == SENSITIVITY_PRIVATE


def test_sensitivity_survives_restart(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()

    mgr = _manager(tmp_path, _FakeRag())
    mgr.add_directory(str(vault), sensitivity="private")

    reloaded = _manager(tmp_path, _FakeRag())
    assert reloaded.sensitivity_for(str(vault / "note.md")) == SENSITIVITY_PRIVATE


def test_nested_directory_keeps_its_own_label(tmp_path):
    """Longest match wins, so a private vault inside a public tree stays private
    no matter which walk reaches the file."""
    outer = tmp_path / "notes"
    inner = outer / "journal"
    inner.mkdir(parents=True)

    mgr = _manager(tmp_path, _FakeRag())
    mgr.add_directory(str(outer), sensitivity="public")
    mgr.add_directory(str(inner), sensitivity="private")

    assert mgr.sensitivity_for(str(outer / "public.md")) == SENSITIVITY_PUBLIC
    assert mgr.sensitivity_for(str(inner / "private.md")) == SENSITIVITY_PRIVATE


def test_untracked_paths_are_public(tmp_path):
    mgr = _manager(tmp_path, _FakeRag())
    assert mgr.sensitivity_for(str(tmp_path / "whatever.md")) == SENSITIVITY_PUBLIC


def test_sibling_prefix_does_not_inherit_label(tmp_path):
    vault = tmp_path / "vault"
    sibling = tmp_path / "vault2"
    vault.mkdir()
    sibling.mkdir()

    mgr = _manager(tmp_path, _FakeRag())
    mgr.add_directory(str(vault), sensitivity="private")

    assert mgr.sensitivity_for(str(sibling / "note.md")) == SENSITIVITY_PUBLIC


def test_relabel_reaches_the_vector_store(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    rag = _FakeRag()
    mgr = _manager(tmp_path, rag)
    mgr.add_directory(str(vault), sensitivity="public")

    result = mgr.set_directory_sensitivity(str(vault), "private")

    assert rag.relabelled == [(os.path.abspath(str(vault)), SENSITIVITY_PRIVATE)]
    assert result["updated_count"] == 3
    assert mgr.sensitivity_for(str(vault / "note.md")) == SENSITIVITY_PRIVATE


def test_remove_directory_drops_the_label(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    mgr = _manager(tmp_path, _FakeRag())
    mgr.add_directory(str(vault), sensitivity="private")

    mgr.remove_directory(str(vault))

    assert os.path.abspath(str(vault)) not in mgr.directory_sensitivity


def test_rename_directory_carries_the_label(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()

    mgr = _manager(tmp_path, _FakeRag())
    mgr.add_directory(str(old), sensitivity="private")
    mgr.rename_directory(str(old), str(new))

    assert mgr.sensitivity_for(str(new / "note.md")) == SENSITIVITY_PRIVATE


def test_tracker_state_files_are_never_indexed(tmp_path):
    """directory_sensitivity.json maps private paths to labels and sits inside
    PERSONAL_DIR, matching the .json extension filter. Indexing it would publish
    the location and existence of every private tree to any model that can read
    public documents."""
    from src.index_walk import STATE_FILENAMES, is_indexable_file

    for name in STATE_FILENAMES:
        assert is_indexable_file(name) is False, name
    assert is_indexable_file("notes.json") is True

    personal = tmp_path / "personal"
    personal.mkdir()
    vault = personal / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("real content", encoding="utf-8")

    mgr = personal_docs.PersonalDocsManager(str(personal), rag_manager=_FakeRag())
    mgr.add_directory(str(vault), index=False, sensitivity="private")

    # The manager has now written its state files into personal/.
    assert (personal / "directory_sensitivity.json").exists()

    mgr.refresh_index()
    indexed = {os.path.basename(f["path"]) for f in mgr.index}
    assert not (indexed & STATE_FILENAMES), f"tracker state leaked into the index: {indexed}"


class TestPrivatePathsAreClosedToFileTools:
    """Private documents live under PERSONAL_DIR, which is inside DATA_DIR — an
    allowed file-tool root. Without an explicit block, any model could read a
    private note by absolute path and bypass the RAG sensitivity filter.

    Retrieval must stay unaffected: indexing and search read from disk directly
    and never consult _is_sensitive_path, so local models keep searching and
    citing private documents; they just cannot open them as files.
    """

    @staticmethod
    def _seed(tmp_path, monkeypatch, label):
        personal = tmp_path / "personal"
        personal.mkdir()
        private = personal / "Journal"
        private.mkdir()
        note = private / "2026-08-01.md"
        note.write_text("private reflection", encoding="utf-8")

        import src.constants as constants
        import src.rag_sensitivity as sensitivity

        monkeypatch.setattr(constants, "PERSONAL_DIR", str(personal))
        # Defeat the mtime cache between parametrised runs.
        sensitivity._private_dirs_cache["mtime"] = None
        sensitivity._private_dirs_cache["dirs"] = ()

        mgr = personal_docs.PersonalDocsManager(str(personal), rag_manager=_FakeRag())
        mgr.add_directory(str(private), index=False, sensitivity=label)
        return str(note), str(private)

    def test_private_note_is_blocked(self, tmp_path, monkeypatch):
        from src.tool_execution import _is_sensitive_path

        note, _ = self._seed(tmp_path, monkeypatch, "private")
        assert _is_sensitive_path(note) is True

    def test_public_note_is_not_blocked(self, tmp_path, monkeypatch):
        from src.tool_execution import _is_sensitive_path

        note, _ = self._seed(tmp_path, monkeypatch, "public")
        assert _is_sensitive_path(note) is False

    def test_sibling_prefix_is_not_blocked(self, tmp_path, monkeypatch):
        """A private /Journal must not also close off /Journal2."""
        from src.tool_execution import _is_sensitive_path

        _, private = self._seed(tmp_path, monkeypatch, "private")
        sibling = private + "2"
        os.makedirs(sibling, exist_ok=True)
        assert _is_sensitive_path(os.path.join(sibling, "note.md")) is False

    def test_corrupt_state_keeps_the_previous_private_set(self, tmp_path, monkeypatch):
        """A partial or corrupt write must not silently unprotect content."""
        import src.rag_sensitivity as sensitivity
        from src.tool_execution import _is_sensitive_path

        note, _ = self._seed(tmp_path, monkeypatch, "private")
        assert _is_sensitive_path(note) is True

        state = tmp_path / "personal" / sensitivity.SENSITIVITY_STATE_FILENAME
        state.write_text("{ this is not json", encoding="utf-8")
        os.utime(state, (0, 0))  # force a cache miss

        assert _is_sensitive_path(note) is True, "must not fall open on corrupt state"

    def test_retrieval_is_unaffected_by_the_file_tool_block(self, tmp_path, monkeypatch):
        """The whole point of the user's qualifier: local models keep using
        embedding/context tools on private trees."""
        note, private = self._seed(tmp_path, monkeypatch, "private")

        mgr = personal_docs.PersonalDocsManager(str(tmp_path / "personal"), rag_manager=_FakeRag())
        mgr.refresh_index()
        names = [f["name"] for f in mgr.get_file_list(allow_private=True)]
        assert any("2026-08-01" in n for n in names), names

        found = mgr.retrieve("private reflection", k=5, allow_private=True)
        assert any("reflection" in hit for hit in found), found


def test_get_file_list_hides_private(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "diary.md").write_text("private", encoding="utf-8")
    public = tmp_path / "public"
    public.mkdir()
    (public / "readme.md").write_text("public", encoding="utf-8")

    mgr = _manager(tmp_path, _FakeRag())
    mgr.add_directory(str(vault), sensitivity="private")
    mgr.add_directory(str(public), sensitivity="public")

    visible = {f["name"] for f in mgr.get_file_list(allow_private=False)}
    assert not any("diary" in name for name in visible), visible
    assert any("readme" in name for name in visible), visible

    everything = {f["name"] for f in mgr.get_file_list(allow_private=True)}
    assert any("diary" in name for name in everything)


# --------------------------------------------------------------------------- #
# Chat retrieval gating
# --------------------------------------------------------------------------- #


class _Session:
    def __init__(self, endpoint_url):
        self.endpoint_url = endpoint_url
        self.model = "m"
        self.headers = {}


class _RecordingRag:
    def __init__(self):
        self.calls = []

    def search(self, query, k=5, owner=None, allow_private=True):
        self.calls.append(allow_private)
        return []


class _Docs:
    def __init__(self, rag):
        self.rag_manager = rag


@pytest.fixture
def _unconfigured_endpoints(monkeypatch):
    """Force host-based classification (no ModelEndpoint rows in play)."""
    monkeypatch.setattr(model_context, "_configured_endpoint_kind", lambda url: None)


def _allow_private_for(endpoint_url):
    rag = _RecordingRag()
    processor = chat_processor.ChatProcessor(
        memory_manager=None, personal_docs_manager=_Docs(rag)
    )
    processor.build_context_preface(
        "what did I write about the budget",
        _Session(endpoint_url),
        use_web=False,
        use_rag=True,
        use_memory=False,
        use_skills=False,
    )
    assert len(rag.calls) == 1
    return rag.calls[0]


@pytest.mark.parametrize(
    "endpoint_url,expected_allow_private",
    [
        ("http://localhost:8080/v1/chat/completions", True),
        ("http://127.0.0.1:8080/v1/chat/completions", True),
        # A model served elsewhere on the LAN is still local for this purpose:
        # the request never leaves the user's network.
        ("http://192.168.1.50:8080/v1/chat/completions", True),
        ("http://10.0.0.7:8000/v1/chat/completions", True),
        ("http://172.16.4.2:8080/v1/chat/completions", True),
        # Tailscale CGNAT range, per model_context._TAILSCALE_CGNAT.
        ("http://100.101.102.103:8080/v1/chat/completions", True),
        # ...but 100.x outside 100.64/10 is public address space.
        ("http://100.20.30.40:8080/v1/chat/completions", False),
        ("https://api.anthropic.com/v1/messages", False),
        ("https://api.openai.com/v1/chat/completions", False),
        ("", False),
    ],
)
def test_chat_retrieval_gates_on_endpoint(
    _unconfigured_endpoints, endpoint_url, expected_allow_private
):
    assert _allow_private_for(endpoint_url) is expected_allow_private


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "http://gpu-box.lan:8080/v1/chat/completions",
        "http://nas.local:8080/v1/chat/completions",
        "http://workstation:8080/v1/chat/completions",
    ],
)
def test_lan_host_by_name_is_not_local_without_configuration(
    _unconfigured_endpoints, endpoint_url
):
    """A LAN box addressed by hostname cannot be classified from the URL alone —
    the name could resolve anywhere. It fails closed, which means a private
    vault would be invisible to the user's own model until the endpoint is
    marked local (see the next test) or addressed by IP.
    """
    assert _allow_private_for(endpoint_url) is False


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "http://gpu-box.lan:8080/v1/chat/completions",
        "https://llm.example.com/v1/chat/completions",
    ],
)
def test_endpoint_kind_local_overrides_host_classification(monkeypatch, endpoint_url):
    """Marking the endpoint `local` in its configuration is the supported way to
    declare a LAN host that host-based classification cannot recognise."""
    monkeypatch.setattr(model_context, "_configured_endpoint_kind", lambda url: "local")
    assert _allow_private_for(endpoint_url) is True


def test_endpoint_kind_api_overrides_a_local_looking_url(monkeypatch):
    """The inverse also has to hold: a proxy on localhost that forwards to a
    hosted provider must not be treated as local."""
    monkeypatch.setattr(model_context, "_configured_endpoint_kind", lambda url: "api")
    assert _allow_private_for("http://localhost:8080/v1/chat/completions") is False


# --------------------------------------------------------------------------- #
# Tool surface
# --------------------------------------------------------------------------- #


class _ToolDocs:
    def get_file_list(self, allow_private=True):
        files = [{"name": "readme.md", "size": 1, "sensitivity": SENSITIVITY_PUBLIC}]
        if allow_private:
            files.append({"name": "diary.md", "size": 1, "sensitivity": SENSITIVITY_PRIVATE})
        return files

    def get_indexed_directories_with_sensitivity(self, allow_private=True):
        dirs = [{"directory": "/a/public", "sensitivity": SENSITIVITY_PUBLIC}]
        if allow_private:
            dirs.append({"directory": "/a/vault", "sensitivity": SENSITIVITY_PRIVATE})
        return dirs


class _SessionManager:
    def __init__(self, endpoint_url):
        self._session = _Session(endpoint_url)

    def get_session(self, session_id):
        return self._session


async def test_tool_list_hides_private_for_api_session(_unconfigured_endpoints, monkeypatch):
    monkeypatch.setattr(ai, "_personal_docs_manager", _ToolDocs())
    monkeypatch.setattr(ai, "_session_manager", _SessionManager("https://api.anthropic.com/v1/messages"))

    result = await ai.do_manage_rag("list", session_id="s1")

    assert "diary.md" not in result["results"]
    assert "/a/vault" not in result["results"]
    assert "readme.md" in result["results"]


async def test_tool_list_shows_private_for_local_session(_unconfigured_endpoints, monkeypatch):
    monkeypatch.setattr(ai, "_personal_docs_manager", _ToolDocs())
    monkeypatch.setattr(ai, "_session_manager", _SessionManager("http://localhost:8080/v1"))

    result = await ai.do_manage_rag("list", session_id="s1")

    assert "diary.md" in result["results"]
    assert "/a/vault" in result["results"]


async def test_tool_list_fails_closed_without_a_session(monkeypatch):
    """No session means no endpoint to classify — assume it is not local."""
    monkeypatch.setattr(ai, "_personal_docs_manager", _ToolDocs())
    monkeypatch.setattr(ai, "_session_manager", None)

    result = await ai.do_manage_rag("list")

    assert "diary.md" not in result["results"]


async def test_tool_add_directory_forwards_label(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    recorded = {}

    class _Rag:
        def index_personal_documents(self, directory, sensitivity=None):
            recorded["sensitivity"] = sensitivity
            return {"indexed_count": 1}

    monkeypatch.setattr(ai, "_rag_manager", _Rag())
    monkeypatch.setattr(ai, "_personal_docs_manager", None)

    result = await ai.do_manage_rag(f"add_directory\n{vault}\nprivate")

    assert recorded["sensitivity"] == SENSITIVITY_PRIVATE
    assert result["sensitivity"] == SENSITIVITY_PRIVATE


async def test_tool_add_directory_defaults_to_public(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    recorded = {}

    class _Rag:
        def index_personal_documents(self, directory, sensitivity=None):
            recorded["sensitivity"] = sensitivity
            return {"indexed_count": 1}

    monkeypatch.setattr(ai, "_rag_manager", _Rag())
    monkeypatch.setattr(ai, "_personal_docs_manager", None)

    await ai.do_manage_rag(f"add_directory\n{vault}")

    assert recorded["sensitivity"] == SENSITIVITY_PUBLIC
