"""ODYSSEUS_PERSONAL_DIRS: declarative personal-document directories.

The variable exists so a compose file alone reproduces a working install, which
means its failure modes matter more than its happy path. Two properties carry
the weight:

  1. A malformed label never becomes "public". normalize_sensitivity coerces
     unknown input to public by design; this parser must NOT, or a typo
     ("Journal:privat") silently publishes a private tree.
  2. Nothing is indexed ownerless. Owner-scoped search would never return such
     chunks, so a missing owner has to skip the work loudly rather than write
     unretrievable rows.

Hermetic — no chromadb, no HTTP; the manager runs against a fake RAG.
"""
import json
import os

import pytest

from src import personal_docs
from src.personal_dirs_config import (
    ENV_VAR,
    parse_declarations,
    reconcile,
    resolve_declared_path,
    resolve_owner,
)


class _FakeRag:
    """Records what it was asked to index, with the label it was given."""

    def __init__(self):
        self.indexed = []
        self.relabelled = []

    def index_personal_documents(self, directory, owner=None, sensitivity=None):
        self.indexed.append(
            {"directory": os.path.abspath(directory), "owner": owner, "sensitivity": sensitivity}
        )
        return {"success": True, "indexed_count": 1}

    def set_directory_sensitivity(self, directory, sensitivity):
        self.relabelled.append((os.path.abspath(directory), sensitivity))
        return {"updated_count": 1}


@pytest.fixture
def personal(tmp_path):
    root = tmp_path / "personal_docs"
    root.mkdir()
    return root


def _seed_auth(tmp_path, monkeypatch, users=None):
    """Point AUTH_FILE at a temp auth.json so owner resolution is deterministic."""
    if users is None:
        users = {"admin": {"is_admin": True}}
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"users": users}), encoding="utf-8")
    import src.constants as constants

    monkeypatch.setattr(constants, "AUTH_FILE", str(auth))
    return auth


def _make_dir(personal, name, body="marker text"):
    d = personal / name
    d.mkdir()
    (d / "note.md").write_text(body, encoding="utf-8")
    return d


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def test_parses_multiple_entries_including_spaces_in_paths():
    entries, errors = parse_declarations("Vault Mind:public,AI Mind:public,Journal:private")
    assert errors == []
    assert [(e.path, e.sensitivity) for e in entries] == [
        ("Vault Mind", "public"),
        ("AI Mind", "public"),
        ("Journal", "private"),
    ]


@pytest.mark.parametrize("label", ["privat", "PRIVATE-ISH", "secret", "confidential", "1"])
def test_unknown_label_is_rejected_not_coerced_to_public(label):
    """The whole point: normalize_sensitivity would make these 'public'."""
    entries, errors = parse_declarations(f"Journal:{label}")
    assert entries == [], f"{label!r} must not be indexed at all"
    assert len(errors) == 1 and "unknown sensitivity" in errors[0]


def test_missing_label_is_rejected():
    entries, errors = parse_declarations("Journal")
    assert entries == []
    assert "no sensitivity label" in errors[0]


def test_label_is_case_and_space_insensitive():
    entries, errors = parse_declarations("  Journal : PRIVATE ")
    assert errors == []
    assert entries[0].sensitivity == "private"
    assert entries[0].path == "Journal"


def test_one_bad_entry_does_not_discard_the_good_ones():
    entries, errors = parse_declarations("Vault:public,Journal:oops,Notes:private")
    assert [e.path for e in entries] == ["Vault", "Notes"]
    assert len(errors) == 1


def test_windows_style_absolute_path_keeps_its_drive_colon():
    entries, errors = parse_declarations(r"C:\vault\Journal:private")
    assert errors == []
    assert entries[0].path == r"C:\vault\Journal"
    assert entries[0].sensitivity == "private"


@pytest.mark.parametrize("raw", [None, "", "   ", ",,"])
def test_empty_input_yields_nothing(raw):
    assert parse_declarations(raw) == ([], [])


# --------------------------------------------------------------------------- #
# Path confinement
# --------------------------------------------------------------------------- #


def test_relative_path_resolves_under_personal_dir(personal):
    resolved = resolve_declared_path("Journal", str(personal))
    assert resolved == os.path.realpath(str(personal / "Journal"))


def test_escaping_path_is_rejected(personal):
    assert resolve_declared_path("../../etc", str(personal)) is None


def test_absolute_path_outside_personal_dir_is_rejected(personal, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert resolve_declared_path(str(outside), str(personal)) is None


def test_sibling_prefix_is_not_treated_as_inside(personal, tmp_path):
    """`personal_docs2` shares a string prefix with `personal_docs`."""
    sibling = tmp_path / "personal_docs2"
    sibling.mkdir()
    assert resolve_declared_path(str(sibling), str(personal)) is None


# --------------------------------------------------------------------------- #
# Owner resolution
# --------------------------------------------------------------------------- #


def test_owner_prefers_the_flagged_admin(tmp_path, monkeypatch):
    _seed_auth(tmp_path, monkeypatch, {"bob": {}, "alice": {"is_admin": True}})
    assert resolve_owner() == "alice"


def test_owner_falls_back_to_first_user(tmp_path, monkeypatch):
    _seed_auth(tmp_path, monkeypatch, {"bob": {}, "carol": {}})
    assert resolve_owner() == "bob"


def test_owner_is_none_without_auth_file(tmp_path, monkeypatch):
    import src.constants as constants

    monkeypatch.setattr(constants, "AUTH_FILE", str(tmp_path / "missing.json"))
    assert resolve_owner() is None


def test_explicit_owner_wins(tmp_path, monkeypatch):
    _seed_auth(tmp_path, monkeypatch)
    assert resolve_owner("dave") == "dave"


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #


def test_declared_directory_is_indexed_with_label_and_owner(personal, tmp_path, monkeypatch):
    _seed_auth(tmp_path, monkeypatch)
    _make_dir(personal, "Journal")
    rag = _FakeRag()
    mgr = personal_docs.PersonalDocsManager(str(personal), rag_manager=rag)

    summary = reconcile(mgr, "Journal:private")

    resolved = os.path.realpath(str(personal / "Journal"))
    assert summary["errors"] == []
    assert [d["directory"] for d in summary["added"]] == [resolved]
    assert mgr.directory_sensitivity[resolved] == "private"
    assert rag.indexed == [
        {"directory": resolved, "owner": "admin", "sensitivity": "private"}
    ]


def test_chunks_are_never_written_ownerless(personal, tmp_path, monkeypatch):
    """No auth.json yet -> skip entirely rather than index unretrievable rows."""
    import src.constants as constants

    monkeypatch.setattr(constants, "AUTH_FILE", str(tmp_path / "missing.json"))
    _make_dir(personal, "Journal")
    rag = _FakeRag()
    mgr = personal_docs.PersonalDocsManager(str(personal), rag_manager=rag)

    summary = reconcile(mgr, "Journal:private")

    assert rag.indexed == []
    assert mgr.indexed_directories == []
    assert any("owner" in e for e in summary["errors"])


def test_rerun_is_idempotent(personal, tmp_path, monkeypatch):
    _seed_auth(tmp_path, monkeypatch)
    _make_dir(personal, "Journal")
    rag = _FakeRag()
    mgr = personal_docs.PersonalDocsManager(str(personal), rag_manager=rag)

    reconcile(mgr, "Journal:private")
    rag.indexed.clear()
    summary = reconcile(mgr, "Journal:private")

    assert rag.indexed == [], "a restart must not re-index unchanged directories"
    assert len(summary["unchanged"]) == 1


def test_label_drift_is_corrected_and_restamps_chunks(personal, tmp_path, monkeypatch):
    """Tightening public -> private has to reach chunks already in the store."""
    _seed_auth(tmp_path, monkeypatch)
    _make_dir(personal, "Journal")
    rag = _FakeRag()
    mgr = personal_docs.PersonalDocsManager(str(personal), rag_manager=rag)

    reconcile(mgr, "Journal:public")
    summary = reconcile(mgr, "Journal:private")

    resolved = os.path.realpath(str(personal / "Journal"))
    assert summary["relabelled"] == [
        {"directory": resolved, "from": "public", "to": "private"}
    ]
    assert (resolved, "private") in rag.relabelled
    assert mgr.directory_sensitivity[resolved] == "private"


def test_typo_leaves_the_directory_unindexed(personal, tmp_path, monkeypatch):
    """Fails closed: not indexed at all beats indexed public."""
    _seed_auth(tmp_path, monkeypatch)
    _make_dir(personal, "Journal")
    rag = _FakeRag()
    mgr = personal_docs.PersonalDocsManager(str(personal), rag_manager=rag)

    summary = reconcile(mgr, "Journal:privat")

    assert rag.indexed == []
    assert mgr.indexed_directories == []
    assert summary["errors"]


def test_untracked_api_added_directories_are_left_alone(personal, tmp_path, monkeypatch):
    """Additive: the variable declares what must exist, not the full set."""
    _seed_auth(tmp_path, monkeypatch)
    _make_dir(personal, "Journal")
    _make_dir(personal, "Manual")
    rag = _FakeRag()
    mgr = personal_docs.PersonalDocsManager(str(personal), rag_manager=rag)
    mgr.add_directory(str(personal / "Manual"), index=False, sensitivity="public")

    reconcile(mgr, "Journal:private")

    assert os.path.abspath(str(personal / "Manual")) in mgr.indexed_directories


def test_missing_mount_is_reported_not_created(personal, tmp_path, monkeypatch):
    _seed_auth(tmp_path, monkeypatch)
    rag = _FakeRag()
    mgr = personal_docs.PersonalDocsManager(str(personal), rag_manager=rag)

    summary = reconcile(mgr, "Journal:private")

    assert rag.indexed == []
    assert any("not a directory" in e for e in summary["errors"])
    assert not (personal / "Journal").exists()


def test_reads_the_environment_when_no_value_is_passed(personal, tmp_path, monkeypatch):
    _seed_auth(tmp_path, monkeypatch)
    _make_dir(personal, "Journal")
    monkeypatch.setenv(ENV_VAR, "Journal:private")
    rag = _FakeRag()
    mgr = personal_docs.PersonalDocsManager(str(personal), rag_manager=rag)

    summary = reconcile(mgr)

    assert len(summary["added"]) == 1


def test_unset_variable_is_a_no_op(personal, tmp_path, monkeypatch):
    _seed_auth(tmp_path, monkeypatch)
    monkeypatch.delenv(ENV_VAR, raising=False)
    rag = _FakeRag()
    mgr = personal_docs.PersonalDocsManager(str(personal), rag_manager=rag)

    assert reconcile(mgr) == {
        "added": [],
        "relabelled": [],
        "unchanged": [],
        "errors": [],
    }
    assert rag.indexed == []
