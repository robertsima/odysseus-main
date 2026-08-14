"""Incremental vault re-index.

A vault indexed once goes stale the moment anything writes to it — the AI
updating its own notes, or a save made outside the app. These cover the three
transitions that matter (new / edited / deleted), plus the two properties that
make re-indexing safe: old chunks are dropped before new ones are written, and
the owner + sensitivity of the original are preserved.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import src.personal_docs as personal_docs
import src.vault_scan as vault_scan
from src.rag_sensitivity import SENSITIVITY_PRIVATE, SENSITIVITY_PUBLIC


class _FakeRag:
    """Chunk store keyed by source path, recording index/delete calls."""

    def __init__(self, owner="admin"):
        self.chunks = {}          # source -> (owner, sensitivity, generation)
        self.deleted = []
        self.indexed = []
        self._owner = owner
        self._generation = 0

    def index_personal_documents(self, directory, owner=None, sensitivity=None):
        return {"success": True, "indexed_count": 0}

    def index_file(self, path, owner=None, sensitivity=None):
        self._generation += 1
        self.chunks[path] = (owner, sensitivity, self._generation)
        self.indexed.append(path)
        return (1, 0)

    def delete_by_source(self, source):
        self.deleted.append(source)
        return 1 if self.chunks.pop(source, None) else 0

    def owner_for_directory(self, directory):
        return self._owner


def _setup(tmp_path, label="public"):
    personal = tmp_path / "personal"
    personal.mkdir()
    vault = personal / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("original body", encoding="utf-8")

    rag = _FakeRag()
    mgr = personal_docs.PersonalDocsManager(str(personal), rag_manager=rag)
    mgr.add_directory(str(vault), index=False, sensitivity=label)
    scanner = vault_scan.VaultScanner(mgr, rag)
    return scanner, rag, note, mgr


def _touch(path, text):
    """Rewrite with a distinct mtime — same-second writes can otherwise share
    an mtime and hide a real change from the scanner."""
    path.write_text(text, encoding="utf-8")
    stat = path.stat()
    os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))


def test_first_scan_indexes_everything(tmp_path):
    scanner, rag, note, _ = _setup(tmp_path)
    result = scanner.scan()
    assert result["reindexed"] == 1
    assert str(note) in [os.path.abspath(p) for p in rag.indexed]


def test_unchanged_files_are_not_reindexed(tmp_path):
    """The point of mtime tracking: a scan over a quiet vault must not
    re-embed anything."""
    scanner, rag, _note, _ = _setup(tmp_path)
    scanner.scan()
    rag.indexed.clear()

    result = scanner.scan()

    assert result["reindexed"] == 0
    assert rag.indexed == []


def test_edited_file_is_reindexed(tmp_path):
    scanner, rag, note, _ = _setup(tmp_path)
    scanner.scan()
    rag.indexed.clear()
    rag.deleted.clear()

    _touch(note, "edited body")
    result = scanner.scan()

    assert result["reindexed"] == 1
    assert os.path.abspath(str(note)) in [os.path.abspath(p) for p in rag.indexed]


def test_edit_deletes_old_chunks_before_reindexing(tmp_path):
    """Chunk ids are content-derived, so without an explicit delete the previous
    version's chunks linger as orphans that still match searches."""
    scanner, rag, note, _ = _setup(tmp_path)
    scanner.scan()
    rag.deleted.clear()

    _touch(note, "edited body")
    scanner.scan()

    assert os.path.abspath(str(note)) in [os.path.abspath(p) for p in rag.deleted]


def test_new_file_is_picked_up(tmp_path):
    scanner, rag, note, _ = _setup(tmp_path)
    scanner.scan()

    fresh = note.parent / "second.md"
    fresh.write_text("new note", encoding="utf-8")
    result = scanner.scan()

    assert result["reindexed"] == 1
    assert os.path.abspath(str(fresh)) in [os.path.abspath(p) for p in rag.indexed]


def test_deleted_file_has_its_chunks_removed(tmp_path):
    scanner, rag, note, _ = _setup(tmp_path)
    scanner.scan()
    rag.deleted.clear()

    note.unlink()
    result = scanner.scan()

    assert result["removed"] == 1
    assert os.path.abspath(str(note)) in [os.path.abspath(p) for p in rag.deleted]


def test_reindex_preserves_sensitivity(tmp_path):
    """A private note must not silently come back public after an edit."""
    scanner, rag, note, _ = _setup(tmp_path, label="private")
    scanner.scan()

    _touch(note, "edited private body")
    scanner.scan()

    _owner, sensitivity, _gen = rag.chunks[os.path.abspath(str(note))]
    assert sensitivity == SENSITIVITY_PRIVATE


def test_reindex_preserves_owner(tmp_path):
    """Owner-filtered search drops ownerless chunks, so losing the owner on
    re-index would make the file silently unretrievable."""
    scanner, rag, note, _ = _setup(tmp_path)
    scanner.scan()

    _touch(note, "edited body")
    scanner.scan()

    owner, _sensitivity, _gen = rag.chunks[os.path.abspath(str(note))]
    assert owner == "admin"


def test_scan_state_survives_restart(tmp_path):
    """A restart must not trigger a full re-embed of an unchanged vault."""
    scanner, rag, _note, mgr = _setup(tmp_path)
    scanner.scan()

    revived = vault_scan.VaultScanner(mgr, rag)
    rag.indexed.clear()
    result = revived.scan()

    assert result["reindexed"] == 0


def test_state_file_is_never_indexed(tmp_path):
    """It lives in PERSONAL_DIR next to the documents; dot-prefixed so
    index_walk skips it."""
    from src.index_walk import is_indexable_file

    assert is_indexable_file(vault_scan.STATE_FILENAME) is False


def test_obsidian_internals_are_not_scanned(tmp_path):
    scanner, rag, note, _ = _setup(tmp_path)
    scanner.scan()
    rag.indexed.clear()

    obsidian = note.parent / ".obsidian"
    obsidian.mkdir()
    (obsidian / "workspace.json").write_text("{}", encoding="utf-8")

    result = scanner.scan()

    assert result["reindexed"] == 0
    assert rag.indexed == []


@pytest.mark.parametrize("raw,expected", [
    (None, vault_scan.DEFAULT_SCAN_INTERVAL_S),
    ("", vault_scan.DEFAULT_SCAN_INTERVAL_S),
    ("60", 60),
    ("0", 0),          # explicit opt-out
    ("1", 5),          # clamped to a sane floor
    ("nonsense", vault_scan.DEFAULT_SCAN_INTERVAL_S),
])
def test_interval_parsing(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("ODYSSEUS_VAULT_SCAN_SECONDS", raising=False)
    else:
        monkeypatch.setenv("ODYSSEUS_VAULT_SCAN_SECONDS", raw)
    assert vault_scan._interval_seconds() == expected


def test_owner_lookup_is_cached_per_directory_within_a_scan(tmp_path, monkeypatch):
    """A migration scan must not re-scan the whole vector store per file.

    owner_for_directory reads every chunk's metadata to find one owner. On an
    ordinary tick a couple of files changed and the cost is invisible; after a
    STATE_VERSION bump every tracked file is changed at once, and an uncached
    lookup turns the one-time re-index into O(files x collection size).
    """
    import src.vault_scan as vault_scan

    vault = tmp_path / "vault"
    (vault / "sub").mkdir(parents=True)
    for name in ("a.md", "b.md", "c.md"):
        (vault / name).write_text("# " + name, encoding="utf-8")
    (vault / "sub" / "d.md").write_text("# d", encoding="utf-8")

    lookups = []

    class _Rag:
        def owner_for_directory(self, directory):
            lookups.append(directory)
            return "rob"

        def delete_by_source(self, path):
            return 0

        def index_file(self, path, owner=None, sensitivity=None):
            return (1, 0)

    class _Manager:
        personal_dir = str(vault)

        def get_indexed_directories(self):
            return []

        def refresh_index(self):
            pass

    scanner = vault_scan.VaultScanner(_Manager(), _Rag())
    result = scanner.scan(extensions={".md"})

    assert result["reindexed"] == 4
    # Two directories, two lookups — not one per file.
    assert len(lookups) == 2
    assert len(set(lookups)) == 2
