"""Filenames are searchable, on every chunk.

A vault often records what a document is *about* only in its name — a journal
entry called ``08-08-2026.md`` typically never repeats its own date in the
prose. Retrieval scores against chunk text (embedding distance plus keyword
overlap), so a name kept only in metadata is unreachable and the entry is
invisible to the obvious query. These cover the header that puts it back in
reach, and the state bump that applies it to an already-indexed vault.
"""
import json
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import src.personal_docs as personal_docs
import src.vault_scan as vault_scan
from src.rag_sensitivity import (
    SENSITIVITY_KEY,
    SENSITIVITY_PRIVATE,
    SENSITIVITY_PUBLIC,
)
from src.rag_vector import VectorRAG, _build_where, _chunk_header


class _RecordingRag(VectorRAG):
    """VectorRAG with the Chroma-backed write replaced by a recorder.

    Built with ``__new__`` so no collection, embedding lane, or network client
    is required — ``index_file`` only needs the batch write and the chunker.
    """

    def __init__(self):
        self.written = []

    def add_documents_batch(self, docs):
        self.written.extend(docs)
        return {"success": True, "added_count": len(docs), "failed_count": 0}


def _rag():
    rag = _RecordingRag.__new__(_RecordingRag)
    rag.__init__()
    return rag


# -- header shape ---------------------------------------------------------


def test_header_emits_bare_stem_as_its_own_token():
    # search() tokenises with a plain str.split(), so "08-08-2026.md" is one
    # token and a query saying "08-08-2026" would not match it. The stem has
    # to appear separately or the header buys nothing for the date query.
    header = _chunk_header("08-08-2026.md")
    assert "08-08-2026" in header.split()


def test_header_does_not_duplicate_an_extensionless_name():
    assert _chunk_header("LICENSE") == "Source: LICENSE"


# -- header application ---------------------------------------------------


def test_every_chunk_carries_the_header_not_just_the_first(tmp_path):
    # The point of the change: a date in the frontmatter or first line only
    # reaches chunk 0, leaving the rest of a long entry unfindable.
    entry = tmp_path / "08-08-2026.md"
    entry.write_text("word " * 2000, encoding="utf-8")

    rag = _rag()
    indexed, failed = rag.index_file(str(entry))

    assert failed == 0
    assert indexed > 1, "fixture should span multiple chunks"
    assert all(text.startswith("Source: 08-08-2026.md") for text, _ in rag.written)
    assert all("08-08-2026" in text.split() for text, _ in rag.written)


def test_body_text_is_preserved_after_the_header(tmp_path):
    entry = tmp_path / "note.md"
    entry.write_text("the body survives", encoding="utf-8")

    rag = _rag()
    rag.index_file(str(entry))

    text, _ = rag.written[0]
    assert text.endswith("the body survives")


def test_identical_bodies_in_different_files_stay_distinct(tmp_path):
    # Chunk ids are content-derived, so before the header two files with the
    # same text collapsed onto one id and the second was silently dropped.
    first = tmp_path / "01-01-2020.md"
    second = tmp_path / "02-02-2021.md"
    for path in (first, second):
        path.write_text("same body", encoding="utf-8")

    rag = _rag()
    rag.index_file(str(first))
    rag.index_file(str(second))

    assert rag.written[0][0] != rag.written[1][0]


# -- migration of an already-indexed vault --------------------------------


class _FakeRag:
    def __init__(self):
        self.indexed = []
        self.calls = {}

    def index_file(self, path, owner=None, sensitivity=None):
        self.indexed.append(path)
        self.calls[path] = {"owner": owner, "sensitivity": sensitivity}
        return (1, 0)

    def delete_by_source(self, source):
        return 1

    def owner_for_directory(self, directory):
        return "admin"


def _scanner(tmp_path, label=None, nested=False):
    personal = tmp_path / "personal"
    personal.mkdir()
    vault = personal / "vault"
    vault.mkdir()
    target = vault / "Entries" if nested else vault
    if nested:
        target.mkdir()
    (target / "note.md").write_text("body", encoding="utf-8")

    rag = _FakeRag()
    mgr = personal_docs.PersonalDocsManager(str(personal), rag_manager=rag)
    mgr.add_directory(str(vault), index=False, sensitivity=label)
    return vault_scan.VaultScanner(mgr, rag), rag, personal


def test_legacy_state_file_forces_one_full_reindex(tmp_path):
    # A pre-versioning state file is a bare path -> [mtime, size] mapping. Its
    # files are unchanged on disk, so without the version check they would keep
    # their old headerless chunks forever.
    scanner, rag, personal = _scanner(tmp_path)
    scanner.scan()
    indexed_path = rag.indexed[0]

    stat = os.stat(indexed_path)
    legacy = {indexed_path: [stat.st_mtime, stat.st_size]}
    (personal / vault_scan.STATE_FILENAME).write_text(json.dumps(legacy), encoding="utf-8")

    rag.indexed.clear()
    vault_scan.VaultScanner(scanner.manager, rag).scan()

    assert indexed_path in rag.indexed, "v1 state must be discarded, not trusted"


def test_current_state_file_does_not_reindex_unchanged_files(tmp_path):
    scanner, rag, personal = _scanner(tmp_path)
    scanner.scan()
    assert rag.indexed, "first scan indexes everything"

    rag.indexed.clear()
    vault_scan.VaultScanner(scanner.manager, rag).scan()

    assert rag.indexed == [], "unchanged files must not be re-embedded"


# -- the header must not weaken the public/private gate -------------------


def test_header_is_text_only_and_leaves_sensitivity_metadata_intact(tmp_path):
    # The gate keys off metadata, so the header must never reach it: a chunk
    # whose text changed but whose label did not stays behind the same filter.
    entry = tmp_path / "08-08-2026.md"
    entry.write_text("private thoughts", encoding="utf-8")

    rag = _rag()
    rag.index_file(str(entry), owner="admin", sensitivity=SENSITIVITY_PRIVATE)

    text, meta = rag.written[0]
    assert text.startswith("Source: 08-08-2026.md")
    assert meta[SENSITIVITY_KEY] == SENSITIVITY_PRIVATE
    assert meta["owner"] == "admin"
    assert "Source:" not in json.dumps(meta), "header belongs in text, not metadata"


def test_forced_reindex_preserves_private_label_and_owner(tmp_path):
    # The version bump re-indexes an already-indexed vault. If that pass lost
    # the label, a journal would come back public and become retrievable by a
    # hosted API model — the exact leak the sensitivity work exists to prevent.
    scanner, rag, personal = _scanner(tmp_path, label=SENSITIVITY_PRIVATE, nested=True)
    scanner.scan()
    path = rag.indexed[0]
    assert rag.calls[path] == {"owner": "admin", "sensitivity": SENSITIVITY_PRIVATE}

    stat = os.stat(path)
    (personal / vault_scan.STATE_FILENAME).write_text(
        json.dumps({path: [stat.st_mtime, stat.st_size]}), encoding="utf-8"
    )
    rag.indexed.clear()
    rag.calls.clear()

    vault_scan.VaultScanner(scanner.manager, rag).scan()

    assert path in rag.indexed, "legacy state must trigger the migration pass"
    assert rag.calls[path]["sensitivity"] == SENSITIVITY_PRIVATE
    assert rag.calls[path]["owner"] == "admin"


def test_public_only_search_still_filters_on_metadata(tmp_path):
    # allow_private=False is what keeps private chunks out of a prompt bound
    # for a non-local endpoint. It matches sensitivity == public by equality,
    # untouched by anything the header does to chunk text. ``owner`` is
    # accepted but ignored (see _build_where's docstring) — no indexing path
    # has ever stamped it, so filtering on it excluded the whole vault.
    where = _build_where("admin", allow_private=False)
    assert where == {SENSITIVITY_KEY: SENSITIVITY_PUBLIC}

    assert _build_where("admin", allow_private=True) is None


def test_state_file_records_its_format_version(tmp_path):
    scanner, _rag, personal = _scanner(tmp_path)
    scanner.scan()

    stored = json.loads((personal / vault_scan.STATE_FILENAME).read_text(encoding="utf-8"))
    assert stored["version"] == vault_scan.STATE_VERSION
    assert "files" in stored
