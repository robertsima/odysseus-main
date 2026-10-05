"""A relabel or an explicit index label can never make stored chunks more
public than the folder policy (2026-09-28).

``POST /api/personal/directory_sensitivity`` used to rewrite every chunk under
a folder to whatever label it was sent. Setting "Journal" — private by the
``vault_folder_sensitivity`` setting — to public made its chunks pass the
public-only retrieval filter until they were re-indexed, while the UI, which
resolves the policy, snapped back to private. The same happened at every boot
when ``ODYSSEUS_PERSONAL_DIRS`` declared a setting-private folder public.

The guarantees under test:
  * the route refuses (400, naming where the rule lives) to publish a folder
    the setting or the environment declares private, and making a folder
    more private still works;
  * the vector store stamps each chunk with its effective label when a
    folder is made public — a private subfolder, or a note marked private in
    its frontmatter, stays private;
  * an explicit public index label is held to the same floor;
  * a private ``ODYSSEUS_PERSONAL_DIRS`` entry cannot be overwritten through
    the legacy state file, and ties with the setting go to private;
  * the startup heal re-privatises chunks an earlier relabel leaked.

Hermetic: settings are faked, Chroma is a fake collection.
"""
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.personal_routes as personal_routes
import src.rag_sensitivity as sensitivity
import src.rag_vector as rag_vector
from core.middleware import require_admin
from src.auth_helpers import require_user
from src.personal_docs import PersonalDocsManager
from src.rag_sensitivity import (
    SENSITIVITY_KEY,
    SENSITIVITY_PRIVATE,
    SENSITIVITY_PUBLIC,
    declared_folder_policy,
    resolve_sensitivity,
)


class _FakeCollection:
    def __init__(self, rows=()):
        self._ids = [r[0] for r in rows]
        self._metas = [dict(r[1]) for r in rows]
        self._docs = ["" for _ in rows]

    def count(self):
        return len(self._ids)

    def get(self, include=None, ids=None):
        keep = range(len(self._ids)) if ids is None else [
            i for i, doc_id in enumerate(self._ids) if doc_id in set(ids)
        ]
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

    def labels(self):
        return {doc_id: meta.get(SENSITIVITY_KEY) for doc_id, meta in zip(self._ids, self._metas)}


class _FakeLane:
    name = "fake"

    def __init__(self, collection):
        self.collection = collection

    def encode(self, texts):
        return [[0.0] for _ in texts]


def _vector_rag(rows=(), *, lanes=False):
    rag = rag_vector.VectorRAG.__new__(rag_vector.VectorRAG)  # skip Chroma
    rag._collection = _FakeCollection(rows)
    rag._lanes = [_FakeLane(rag._collection)] if lanes else []
    rag._healthy = True
    return rag


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_PERSONAL_DIRS", raising=False)
    for cache in (sensitivity._legacy_state_cache,):
        cache["mtime"] = None
        cache["map"] = {}
    sensitivity._private_dirs_cache["mtime"] = None
    sensitivity._private_dirs_cache["dirs"] = ()
    sensitivity._env_cache["key"] = None
    sensitivity._env_cache["entries"] = ()
    yield


@pytest.fixture
def settings(monkeypatch):
    values = {
        "vault_directory": "",
        "vault_default_sensitivity": SENSITIVITY_PUBLIC,
        "vault_folder_sensitivity": {},
    }
    import src.settings as settings_mod

    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: values.get(key, default))
    return values


@pytest.fixture
def vault(tmp_path, monkeypatch, settings):
    """A vault that is also PERSONAL_DIR, with Journal/ and Notes/Secret/."""
    root = tmp_path / "personal_docs"
    for sub in ("Journal", "Notes/Secret"):
        (root / sub).mkdir(parents=True)
    (root / "Journal" / "day.md").write_text("dear diary", encoding="utf-8")
    (root / "Notes" / "plain.md").write_text("shopping list", encoding="utf-8")
    (root / "Notes" / "marked.md").write_text(
        "---\nsensitivity: private\n---\nmarked private in its own header", encoding="utf-8"
    )
    (root / "Notes" / "Secret" / "plan.md").write_text("the plan", encoding="utf-8")
    import src.constants as constants

    root_real = os.path.realpath(str(root))
    monkeypatch.setattr(constants, "PERSONAL_DIR", root_real)
    monkeypatch.setattr(personal_routes, "PERSONAL_DIR", root_real)
    settings["vault_directory"] = root_real
    return root_real


def _chunk(doc_id, path, label):
    return (doc_id, {"source": path, SENSITIVITY_KEY: label})


def _journal_rows(vault, label):
    return [_chunk("j1", os.path.join(vault, "Journal", "day.md"), label)]


def _client(manager):
    app = FastAPI()
    app.include_router(personal_routes.setup_personal_routes(manager, None, True))
    app.dependency_overrides[require_user] = lambda: "admin"
    app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app)


def _manager(vault, rag):
    return PersonalDocsManager(vault, rag_manager=rag, state_dir=vault)


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #


def test_setting_private_folder_cannot_be_relabelled_public(vault, settings):
    settings["vault_folder_sensitivity"] = {"Journal": "private"}
    rag = _vector_rag(_journal_rows(vault, SENSITIVITY_PRIVATE))
    manager = _manager(vault, rag)
    journal = os.path.join(vault, "Journal")

    resp = _client(manager).post(
        "/api/personal/directory_sensitivity",
        json={"directory": journal, "sensitivity": "public"},
    )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "vault_folder_sensitivity" in detail and '"Journal"' in detail
    assert rag._collection.labels() == {"j1": SENSITIVITY_PRIVATE}
    assert journal not in manager.directory_sensitivity, "the refused label must not be recorded"


def test_env_private_folder_cannot_be_relabelled_public(vault, settings, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_PERSONAL_DIRS", "Notes:public,Journal:private")
    rag = _vector_rag(_journal_rows(vault, SENSITIVITY_PRIVATE))
    manager = _manager(vault, rag)

    resp = _client(manager).post(
        "/api/personal/directory_sensitivity",
        json={"directory": "Journal", "sensitivity": "public"},
    )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "ODYSSEUS_PERSONAL_DIRS" in detail and "Journal:private" in detail and "restart" in detail
    assert rag._collection.labels() == {"j1": SENSITIVITY_PRIVATE}


def test_subfolder_of_a_private_folder_cannot_be_published(vault, settings):
    """The rule covers descendants; relabelling one of them is the same leak."""
    settings["vault_folder_sensitivity"] = {"Notes": "private"}
    rag = _vector_rag([_chunk("s1", os.path.join(vault, "Notes", "Secret", "plan.md"), SENSITIVITY_PRIVATE)])

    resp = _client(_manager(vault, rag)).post(
        "/api/personal/directory_sensitivity",
        json={"directory": os.path.join(vault, "Notes", "Secret"), "sensitivity": "public"},
    )

    assert resp.status_code == 400
    assert rag._collection.labels() == {"s1": SENSITIVITY_PRIVATE}


def test_making_a_folder_more_private_still_works(vault, settings):
    rag = _vector_rag(_journal_rows(vault, SENSITIVITY_PUBLIC))
    manager = _manager(vault, rag)
    journal = os.path.join(vault, "Journal")

    resp = _client(manager).post(
        "/api/personal/directory_sensitivity",
        json={"directory": journal, "sensitivity": "private"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sensitivity"] == SENSITIVITY_PRIVATE and body["updated_count"] == 1
    assert rag._collection.labels() == {"j1": SENSITIVITY_PRIVATE}
    assert resolve_sensitivity(os.path.join(journal, "day.md")) == SENSITIVITY_PRIVATE


def test_undeclared_folder_can_still_be_made_public(vault, settings):
    """The legacy UI label is this route's own layer: a folder only it made
    private can be made public again."""
    rag = _vector_rag(_journal_rows(vault, SENSITIVITY_PUBLIC))
    manager = _manager(vault, rag)
    client = _client(manager)
    journal = os.path.join(vault, "Journal")

    assert client.post("/api/personal/directory_sensitivity",
                       json={"directory": journal, "sensitivity": "private"}).status_code == 200
    sensitivity._legacy_state_cache["mtime"] = None  # two writes inside one mtime tick
    resp = client.post("/api/personal/directory_sensitivity",
                       json={"directory": journal, "sensitivity": "public"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["sensitivity"] == SENSITIVITY_PUBLIC
    assert rag._collection.labels() == {"j1": SENSITIVITY_PUBLIC}


def test_publishing_a_parent_keeps_private_children_at_their_effective_label(vault, settings):
    """Relabelling Notes public: its private subfolder and the note marked
    private in its own frontmatter stay private; chunks that leaked public
    under them are restamped; only the rest becomes public."""
    settings["vault_folder_sensitivity"] = {"Notes/Secret": "private"}
    notes = os.path.join(vault, "Notes")
    rag = _vector_rag([
        _chunk("plain", os.path.join(notes, "plain.md"), SENSITIVITY_PRIVATE),
        _chunk("marked", os.path.join(notes, "marked.md"), SENSITIVITY_PRIVATE),
        _chunk("secret", os.path.join(notes, "Secret", "plan.md"), SENSITIVITY_PUBLIC),  # leaked earlier
        _chunk("journal", os.path.join(vault, "Journal", "day.md"), SENSITIVITY_PRIVATE),
    ])

    resp = _client(_manager(vault, rag)).post(
        "/api/personal/directory_sensitivity",
        json={"directory": notes, "sensitivity": "public"},
    )

    assert resp.status_code == 200, resp.text
    assert rag._collection.labels() == {
        "plain": SENSITIVITY_PUBLIC,
        "marked": SENSITIVITY_PRIVATE,
        "secret": SENSITIVITY_PRIVATE,
        "journal": SENSITIVITY_PRIVATE,  # outside the relabelled folder: untouched
    }


def test_more_private_against_a_public_setting_says_it_will_not_stick(vault, settings):
    settings["vault_folder_sensitivity"] = {"Journal": "public"}
    rag = _vector_rag(_journal_rows(vault, SENSITIVITY_PUBLIC))

    resp = _client(_manager(vault, rag)).post(
        "/api/personal/directory_sensitivity",
        json={"directory": os.path.join(vault, "Journal"), "sensitivity": "private"},
    )

    assert resp.status_code == 200
    body = resp.json()
    # The chunks are tightened (safe), and the response tells the truth about
    # what retrieval and the UI will apply from now on.
    assert rag._collection.labels() == {"j1": SENSITIVITY_PRIVATE}
    assert body["sensitivity"] == SENSITIVITY_PUBLIC
    assert "vault_folder_sensitivity" in body["message"]


def test_add_directory_refuses_explicit_public_and_defaults_to_policy(vault, settings, monkeypatch):
    settings["vault_folder_sensitivity"] = {"Journal": "private"}
    seen = {}

    class _IndexRag:
        def index_personal_documents(self, directory, owner=None, sensitivity=None):
            seen["label"] = sensitivity
            return {"success": True, "indexed_count": 1, "failed_count": 0}

    monkeypatch.setattr(personal_routes, "get_rag_manager", lambda: _IndexRag())
    manager = _manager(vault, None)
    client = _client(manager)
    journal = os.path.join(vault, "Journal")

    refused = client.post("/api/personal/add_directory",
                          json={"directory": journal, "sensitivity": "public"})
    assert refused.status_code == 400 and "vault_folder_sensitivity" in refused.json()["detail"]
    assert "label" not in seen

    added = client.post("/api/personal/add_directory", json={"directory": journal})
    assert added.status_code == 200, added.text
    assert seen["label"] == SENSITIVITY_PRIVATE
    assert added.json()["sensitivity"] == SENSITIVITY_PRIVATE


# --------------------------------------------------------------------------- #
# The policy layers
# --------------------------------------------------------------------------- #


def test_env_private_entry_cannot_be_overwritten_through_the_legacy_file(vault, settings, monkeypatch):
    """An agent's add_directory or a pre-fix UI relabel writes the legacy
    file; a private environment declaration must still win until restart."""
    monkeypatch.setenv("ODYSSEUS_PERSONAL_DIRS", "Journal:private")
    with open(os.path.join(vault, sensitivity.SENSITIVITY_STATE_FILENAME), "w", encoding="utf-8") as f:
        json.dump({os.path.join(vault, "Journal"): "public"}, f)

    note = os.path.join(vault, "Journal", "day.md")
    assert resolve_sensitivity(note) == SENSITIVITY_PRIVATE
    assert sensitivity.path_is_under_private_directory(note) is True
    assert declared_folder_policy(note).source == "ODYSSEUS_PERSONAL_DIRS"


def test_env_private_wins_a_tie_with_the_setting_but_not_a_deeper_carve_out(vault, settings, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_PERSONAL_DIRS", "Journal:private")
    settings["vault_folder_sensitivity"] = {"": "public", "Journal": "public", "Journal/Shared": "public"}

    assert resolve_sensitivity("Journal/day.md") == SENSITIVITY_PRIVATE
    assert resolve_sensitivity("Journal/Shared/ok.md") == SENSITIVITY_PUBLIC
    assert resolve_sensitivity("Notes/plain.md") == SENSITIVITY_PUBLIC


def test_env_public_entry_does_not_override_a_more_private_ui_label(vault, settings, monkeypatch):
    """Only the private direction is read from the environment: an operator
    can still make an env-public folder private from the UI."""
    monkeypatch.setenv("ODYSSEUS_PERSONAL_DIRS", "Notes:public")
    with open(os.path.join(vault, sensitivity.SENSITIVITY_STATE_FILENAME), "w", encoding="utf-8") as f:
        json.dump({os.path.join(vault, "Notes"): "private"}, f)

    assert resolve_sensitivity("Notes/plain.md") == SENSITIVITY_PRIVATE
    assert declared_folder_policy("Notes/plain.md") is None


def test_explicit_public_index_label_is_held_to_the_floor(vault, settings):
    """The vault scan and add_directory pass a folder-level label; a note
    marked private in its own header, or a folder declared private, must not
    be stamped public by it. The vault default is not part of the floor."""
    settings["vault_folder_sensitivity"] = {"Notes/Secret": "private"}
    settings["vault_default_sensitivity"] = SENSITIVITY_PRIVATE
    rag = _vector_rag(lanes=True)
    notes = os.path.join(vault, "Notes")

    for name in ("plain.md", "marked.md", os.path.join("Secret", "plan.md")):
        rag.index_file(os.path.join(notes, name), sensitivity="public")

    by_source = {}
    for meta in rag._collection.get()["metadatas"]:
        by_source.setdefault(os.path.relpath(meta["source"], notes), set()).add(meta[SENSITIVITY_KEY])
    assert by_source == {
        "plain.md": {SENSITIVITY_PUBLIC},
        "marked.md": {SENSITIVITY_PRIVATE},
        os.path.join("Secret", "plan.md"): {SENSITIVITY_PRIVATE},
    }


def test_boot_env_public_entry_cannot_publish_a_setting_private_folder(vault, settings):
    """The boot re-apply relabels through the same floor, and says so."""
    from src.personal_dirs_config import reconcile

    settings["vault_folder_sensitivity"] = {"Journal": "private"}
    rag = _vector_rag(_journal_rows(vault, SENSITIVITY_PRIVATE))
    manager = _manager(vault, rag)
    manager.add_directory(os.path.join(vault, "Journal"), index=False, sensitivity="private")
    sensitivity._legacy_state_cache["mtime"] = None

    summary = reconcile(manager, "Journal:public")

    assert summary["relabelled"], summary
    assert rag._collection.labels() == {"j1": SENSITIVITY_PRIVATE}
    assert summary["conflicts"] and "vault_folder_sensitivity" in summary["conflicts"][0]["policy"]


# --------------------------------------------------------------------------- #
# Startup heal
# --------------------------------------------------------------------------- #


def test_startup_heal_reprivatises_leaked_chunks(vault, settings, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_PERSONAL_DIRS", "Notes/Secret:private")
    settings["vault_folder_sensitivity"] = {"Journal": "private"}
    (open(os.path.join(vault, "Journal", "shared.md"), "w", encoding="utf-8")
     .write("---\nsensitivity: public\n---\nfine to share"))
    rag = _vector_rag([
        _chunk("leaked", os.path.join(vault, "Journal", "day.md"), SENSITIVITY_PUBLIC),
        _chunk("exception", os.path.join(vault, "Journal", "shared.md"), SENSITIVITY_PUBLIC),
        _chunk("env", os.path.join(vault, "Notes", "Secret", "plan.md"), SENSITIVITY_PUBLIC),
        _chunk("public", os.path.join(vault, "Notes", "plain.md"), SENSITIVITY_PUBLIC),
    ], lanes=True)

    result = rag.enforce_sensitivity_policy()

    assert result["updated_count"] == 2
    assert rag._collection.labels() == {
        "leaked": SENSITIVITY_PRIVATE,
        "exception": SENSITIVITY_PUBLIC,  # its own frontmatter says public
        "env": SENSITIVITY_PRIVATE,
        "public": SENSITIVITY_PUBLIC,
    }


def test_startup_heal_skips_a_malformed_setting(vault, settings):
    """Nothing demotes a chunk again, so a fail-closed answer must not strand
    the whole index as private."""
    settings["vault_folder_sensitivity"] = ["not", "an", "object"]
    rag = _vector_rag([_chunk("p", os.path.join(vault, "Notes", "plain.md"), SENSITIVITY_PUBLIC)], lanes=True)

    assert rag.enforce_sensitivity_policy()["updated_count"] == 0
    assert rag._collection.labels() == {"p": SENSITIVITY_PUBLIC}
