"""Store-agnostic sensitivity resolution: `resolve_sensitivity` and `vault_root`.

`rag_sensitivity.resolve_sensitivity` extends the original PersonalDocsManager
directory-label story (see ``test_rag_sensitivity.py``) to a vault that can
live anywhere (``vault_directory``), be folder-labelled wholesale
(``vault_folder_sensitivity``), and still honour a per-file frontmatter
override. It has to keep working for content that is not a real file on disk
(a note stored as a DB row), and it has to never let a corrupted admin
setting quietly leak content to a hosted model.

None of this touches `private_directories` / `path_is_under_private_directory`
— those two back the file-tool deny-list and are exercised in
``test_rag_sensitivity.py``; a couple of tests here just pin that they stay
independent of the new settings-driven layer, per that module's docstring on
the four enforcement paths that fail independently by design.

Hermetic — no chromadb, no HTTP; only `src.settings.get_setting` and
`src.constants.PERSONAL_DIR` are monkeypatched.
"""
import json
import os

import pytest

import src.rag_sensitivity as sensitivity
from src.rag_sensitivity import (
    SENSITIVITY_PRIVATE,
    SENSITIVITY_PUBLIC,
    path_is_under_private_directory,
    resolve_sensitivity,
    vault_root,
)


@pytest.fixture(autouse=True)
def _reset_caches():
    """Every cache here is keyed on a state file's mtime; tests reuse the same
    filenames across different tmp_path roots within the same test run, and
    an mtime collision must not serve another test's stale data."""
    sensitivity._private_dirs_cache["mtime"] = None
    sensitivity._private_dirs_cache["dirs"] = ()
    sensitivity._legacy_state_cache["mtime"] = None
    sensitivity._legacy_state_cache["map"] = {}
    yield


@pytest.fixture
def fake_settings(monkeypatch):
    """Stand-in for src.settings.get_setting, defaulting to DEFAULT_SETTINGS'
    values for the three keys resolve_sensitivity reads."""
    values = {
        "vault_directory": "",
        "vault_default_sensitivity": SENSITIVITY_PUBLIC,
        "vault_folder_sensitivity": {},
    }

    def _get_setting(key, default=None):
        return values.get(key, default)

    import src.settings as settings

    monkeypatch.setattr(settings, "get_setting", _get_setting)
    return values


@pytest.fixture
def vault(tmp_path, monkeypatch, fake_settings):
    """A vault rooted at tmp_path, with PERSONAL_DIR pointed at the same place
    so the legacy state file resolves predictably."""
    root = tmp_path / "vault"
    root.mkdir()
    import src.constants as constants

    monkeypatch.setattr(constants, "PERSONAL_DIR", str(root))
    fake_settings["vault_directory"] = str(root)
    return root


def _write_legacy(vault_dir, mapping):
    """Write the legacy directory_sensitivity.json state file that
    PersonalDocsManager writes, keyed by absolute directory.

    Forces the mtime-keyed cache to miss on the next read: a test that writes
    this file twice in quick succession cannot rely on the filesystem's mtime
    resolution alone to notice the second write.
    """
    state = os.path.join(str(vault_dir), sensitivity.SENSITIVITY_STATE_FILENAME)
    with open(state, "w", encoding="utf-8") as f:
        json.dump({os.path.abspath(str(vault_dir / k)): v for k, v in mapping.items()}, f)
    sensitivity._legacy_state_cache["mtime"] = None
    sensitivity._legacy_state_cache["map"] = {}


# --------------------------------------------------------------------------- #
# vault_root
# --------------------------------------------------------------------------- #


def test_vault_root_falls_back_to_personal_dir(monkeypatch, fake_settings, tmp_path):
    import src.constants as constants

    monkeypatch.setattr(constants, "PERSONAL_DIR", str(tmp_path))
    fake_settings["vault_directory"] = ""
    assert vault_root() == str(tmp_path)


def test_vault_root_uses_configured_directory(monkeypatch, fake_settings, tmp_path):
    import src.constants as constants

    monkeypatch.setattr(constants, "PERSONAL_DIR", str(tmp_path / "personal_docs"))
    fake_settings["vault_directory"] = str(tmp_path / "my_vault")
    assert vault_root() == str(tmp_path / "my_vault")


# --------------------------------------------------------------------------- #
# Default (layer 4) and undeclared folders
# --------------------------------------------------------------------------- #


def test_undeclared_path_gets_the_configured_default(vault, fake_settings):
    """An undeclared folder is not hardcoded to private — it takes whatever
    the operator configured as the default, in both directions."""
    (vault / "note.md").write_text("x", encoding="utf-8")

    fake_settings["vault_default_sensitivity"] = SENSITIVITY_PUBLIC
    assert resolve_sensitivity(str(vault / "note.md")) == SENSITIVITY_PUBLIC

    fake_settings["vault_default_sensitivity"] = SENSITIVITY_PRIVATE
    assert resolve_sensitivity(str(vault / "note.md")) == SENSITIVITY_PRIVATE


def test_undeclared_logical_path_also_gets_the_default(vault, fake_settings):
    """A note with no file on disk (a DB row) resolves the same way, given
    only a vault-relative logical path."""
    fake_settings["vault_default_sensitivity"] = SENSITIVITY_PUBLIC
    assert resolve_sensitivity("Inbox/idea.md") == SENSITIVITY_PUBLIC


# --------------------------------------------------------------------------- #
# vault_folder_sensitivity (layer 2) and deepest-match
# --------------------------------------------------------------------------- #


def test_folder_declaration_overrides_default(vault, fake_settings):
    fake_settings["vault_default_sensitivity"] = SENSITIVITY_PUBLIC
    fake_settings["vault_folder_sensitivity"] = {"Journal": "private"}
    (vault / "Journal").mkdir()
    note = vault / "Journal" / "2026-01-01.md"
    note.write_text("private thoughts", encoding="utf-8")

    assert resolve_sensitivity(str(note)) == SENSITIVITY_PRIVATE
    # Same answer given only the logical, vault-relative form.
    assert resolve_sensitivity("Journal/2026-01-01.md") == SENSITIVITY_PRIVATE


def test_deepest_folder_wins_public_subfolder_inside_private_tree(vault, fake_settings):
    fake_settings["vault_folder_sensitivity"] = {
        "Journal": "private",
        "Journal/Shareable": "public",
    }
    assert resolve_sensitivity("Journal/private.md") == SENSITIVITY_PRIVATE
    assert resolve_sensitivity("Journal/Shareable/public.md") == SENSITIVITY_PUBLIC
    # A sibling that merely shares the prefix string must not inherit either.
    fake_settings["vault_folder_sensitivity"] = {"Journal": "private"}
    assert resolve_sensitivity("Journal2/note.md") == SENSITIVITY_PUBLIC


def test_deepest_folder_wins_private_subfolder_inside_public_tree(vault, fake_settings):
    fake_settings["vault_folder_sensitivity"] = {
        "Vault": "public",
        "Vault/Private": "private",
    }
    assert resolve_sensitivity("Vault/readme.md") == SENSITIVITY_PUBLIC
    assert resolve_sensitivity("Vault/Private/secret.md") == SENSITIVITY_PRIVATE


# --------------------------------------------------------------------------- #
# Legacy directory_sensitivity.json (layer 3)
# --------------------------------------------------------------------------- #


def test_legacy_json_declaration_still_honoured(vault, fake_settings):
    """No vault_folder_sensitivity entry at all — the pre-existing per-
    directory state file must still be consulted."""
    (vault / "AI Mind").mkdir()
    _write_legacy(vault, {"AI Mind": "private"})

    assert resolve_sensitivity(str(vault / "AI Mind" / "note.md")) == SENSITIVITY_PRIVATE
    assert resolve_sensitivity("AI Mind/note.md") == SENSITIVITY_PRIVATE


def test_folder_setting_takes_precedence_over_legacy_json(vault, fake_settings):
    """Both layers declare the same folder differently — the newer,
    settings-driven layer (2) wins over the legacy file (3)."""
    (vault / "Mixed").mkdir()
    _write_legacy(vault, {"Mixed": "private"})
    fake_settings["vault_folder_sensitivity"] = {"Mixed": "public"}

    assert resolve_sensitivity(str(vault / "Mixed" / "note.md")) == SENSITIVITY_PUBLIC


# --------------------------------------------------------------------------- #
# Frontmatter override (layer 1) — both directions
# --------------------------------------------------------------------------- #


def test_frontmatter_private_overrides_public_folder(vault, fake_settings):
    fake_settings["vault_folder_sensitivity"] = {"Vault": "public"}
    result = resolve_sensitivity(
        "Vault/one-off-secret.md", frontmatter={"sensitivity": "private"}
    )
    assert result == SENSITIVITY_PRIVATE


def test_frontmatter_public_overrides_private_folder(vault, fake_settings):
    fake_settings["vault_folder_sensitivity"] = {"Journal": "private"}
    result = resolve_sensitivity(
        "Journal/ok-to-share.md", frontmatter={"sensitivity": "public"}
    )
    assert result == SENSITIVITY_PUBLIC


def test_frontmatter_without_the_key_does_not_override(vault, fake_settings):
    fake_settings["vault_folder_sensitivity"] = {"Journal": "private"}
    result = resolve_sensitivity("Journal/note.md", frontmatter={"title": "whatever"})
    assert result == SENSITIVITY_PRIVATE


# --------------------------------------------------------------------------- #
# Full precedence chain, exercised together
# --------------------------------------------------------------------------- #


def test_full_precedence_chain(vault, fake_settings):
    fake_settings["vault_default_sensitivity"] = SENSITIVITY_PUBLIC
    _write_legacy(vault, {"Legacy": "private"})
    fake_settings["vault_folder_sensitivity"] = {
        "Legacy": "public",  # (2) beats the legacy file's "private" for Legacy/
        "Folder": "private",
    }

    # (4) default: nothing declares this path at all.
    assert resolve_sensitivity("Undeclared/x.md") == SENSITIVITY_PUBLIC
    # (3) legacy file: no vault_folder_sensitivity entry for this branch.
    _write_legacy(vault, {"Legacy": "private", "OnlyLegacy": "private"})
    assert resolve_sensitivity("OnlyLegacy/x.md") == SENSITIVITY_PRIVATE
    # (2) settings folder beats the legacy file for the same folder.
    assert resolve_sensitivity("Legacy/x.md") == SENSITIVITY_PUBLIC
    # (1) frontmatter beats everything, in the tightening direction.
    assert resolve_sensitivity(
        "Folder/x.md", frontmatter={"sensitivity": "public"}
    ) == SENSITIVITY_PUBLIC
    assert resolve_sensitivity("Folder/x.md") == SENSITIVITY_PRIVATE


# --------------------------------------------------------------------------- #
# Hostile / malformed vault_folder_sensitivity: fail closed, never crash
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad_value",
    [
        ["Journal:private"],           # not a dict at all
        "Journal:private",             # a string, not a dict
        None,
    ],
)
def test_non_dict_folder_setting_fails_closed(vault, fake_settings, bad_value):
    fake_settings["vault_default_sensitivity"] = SENSITIVITY_PUBLIC  # would leak if honoured
    fake_settings["vault_folder_sensitivity"] = bad_value
    assert resolve_sensitivity("Anything/note.md") == SENSITIVITY_PRIVATE


@pytest.mark.parametrize(
    "bad_map",
    [
        {"Journal": True},          # bool, not the string "private"/"public"
        {"Journal": 1},              # int
        {"Journal": ["private"]},    # nested list
        {"Journal": "sekrit"},       # unrecognised label — must not coerce, must not crash
    ],
)
def test_wrong_value_type_fails_closed(vault, fake_settings, bad_map):
    fake_settings["vault_default_sensitivity"] = SENSITIVITY_PUBLIC
    fake_settings["vault_folder_sensitivity"] = bad_map
    assert resolve_sensitivity("Journal/note.md") == SENSITIVITY_PRIVATE
    # A completely unrelated path is affected too — the whole setting is
    # untrustworthy, not just the one bad entry.
    assert resolve_sensitivity("Unrelated/note.md") == SENSITIVITY_PRIVATE


@pytest.mark.parametrize(
    "traversal_key",
    ["../outside", "../../etc", "Journal/../../escape", "/etc/passwd", "C:/Windows"],
)
def test_path_traversal_key_fails_closed(vault, fake_settings, traversal_key):
    fake_settings["vault_default_sensitivity"] = SENSITIVITY_PUBLIC
    fake_settings["vault_folder_sensitivity"] = {traversal_key: "public"}
    assert resolve_sensitivity("Anything/note.md") == SENSITIVITY_PRIVATE


def test_malformed_folder_setting_never_crashes(vault, fake_settings):
    """Belt and suspenders: every hostile shape above must return a plain
    string, not raise."""
    for bad in (["x"], {"a": {"b": "c"}}, {"..": "private"}, 42, "not a dict"):
        fake_settings["vault_folder_sensitivity"] = bad
        assert resolve_sensitivity("whatever.md") in (SENSITIVITY_PUBLIC, SENSITIVITY_PRIVATE)


def test_frontmatter_still_overrides_a_malformed_folder_setting(vault, fake_settings):
    """A broken admin setting must not be able to override the one thing more
    specific than it: what the file itself explicitly says about itself."""
    fake_settings["vault_folder_sensitivity"] = "not-a-dict"
    result = resolve_sensitivity(
        "Journal/note.md", frontmatter={"sensitivity": "public"}
    )
    assert result == SENSITIVITY_PUBLIC


# --------------------------------------------------------------------------- #
# Independence from private_directories / path_is_under_private_directory
# --------------------------------------------------------------------------- #


def test_file_tool_check_uses_vault_folder_sensitivity(vault, fake_settings):
    """The file-tool guard must use the same folder policy as retrieval."""
    fake_settings["vault_folder_sensitivity"] = {"Journal": "private"}
    (vault / "Journal").mkdir()
    note = vault / "Journal" / "note.md"
    note.write_text("x", encoding="utf-8")

    # resolve_sensitivity sees it as private via the folder setting...
    assert resolve_sensitivity(str(note)) == SENSITIVITY_PRIVATE
    # ...and direct file tools cannot walk around that policy.
    assert path_is_under_private_directory(str(note)) is True


def test_file_tool_check_fails_closed_on_malformed_vault_folder_sensitivity(
    vault, fake_settings
):
    """A broken security policy must not make file tools fall open."""
    fake_settings["vault_folder_sensitivity"] = ["not", "a", "dict"]
    note = vault / "note.md"
    note.write_text("x", encoding="utf-8")

    assert path_is_under_private_directory(str(note)) is True


def test_file_tool_check_honours_public_frontmatter_override(vault, fake_settings):
    fake_settings["vault_folder_sensitivity"] = {"Journal": "private"}
    folder = vault / "Journal"
    folder.mkdir()
    note = folder / "share.md"
    note.write_text("---\nsensitivity: public\n---\nShareable", encoding="utf-8")
    assert path_is_under_private_directory(str(note)) is False


def test_file_tool_check_fails_closed_on_malformed_frontmatter(vault, fake_settings):
    fake_settings["vault_default_sensitivity"] = SENSITIVITY_PUBLIC
    note = vault / "broken.md"
    note.write_text("---\nsensitivity: [unterminated\n---\nsecret", encoding="utf-8")

    assert path_is_under_private_directory(str(note)) is True


def test_frontmatter_sensitivity_key_is_case_insensitive(vault, fake_settings):
    fake_settings["vault_default_sensitivity"] = SENSITIVITY_PUBLIC

    assert resolve_sensitivity(
        "note.md", frontmatter={"Sensitivity": "private"}
    ) == SENSITIVITY_PRIVATE
