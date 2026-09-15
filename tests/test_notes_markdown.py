"""The note <-> Markdown codec has one job: nothing a note carries may be
silently dropped or corrupted by the trip through a text file, no matter how
adversarial the title, content, or checklist text is. These tests exercise
every field in the mapping table from the migration spec, plus the
filename-safety rules that keep a note's title from ever becoming a path.
"""
import os
from datetime import datetime
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

from src.notes_markdown import (
    NoteItem,
    NoteRecord,
    markdown_to_note,
    note_filename,
    note_record_from_orm,
    note_to_markdown,
    resolve_note_directory,
    safe_join,
    slugify_title,
)


def _full_note(**overrides) -> NoteRecord:
    base = dict(
        id="note-0001",
        owner="rob",
        title="Groceries",
        content="Milk\nEggs",
        items=None,
        color="#ffcc00",
        label="home",
        pinned=True,
        archived=False,
        due_date="2026-03-01",
        source="agent",
        session_id="sess-1",
        sort_order=3,
        image_url="uploads/receipt.png",
        repeat="weekly",
        ai_classification='{"kind": "task", "confidence": 0.9}',
        ai_content_hash="abc123hash",
        agent_session_id="agent-sess-1",
        created_at=datetime(2026, 1, 1, 8, 30, 0),
        updated_at=datetime(2026, 1, 2, 9, 45, 30),
    )
    base.update(overrides)
    return NoteRecord(**base)


def _roundtrip(note: NoteRecord) -> NoteRecord:
    text = note_to_markdown(note)
    decoded = markdown_to_note(text)
    # note_to_markdown/markdown_to_note never touch `archived` — it is
    # expressed by which directory the file lives in, a decision made above
    # this codec. Set it back so equality checks focus on everything else.
    decoded.archived = note.archived
    return decoded


# ---------------------------------------------------------------------------
# Round-trip fidelity, field by field
# ---------------------------------------------------------------------------


def test_a_fully_populated_plain_note_round_trips_exactly():
    note = _full_note()
    assert _roundtrip(note) == note


def test_a_checklist_with_items_round_trips():
    note = _full_note(
        content=None,
        items=[NoteItem(text="Buy milk", done=False), NoteItem(text="Pay rent", done=True)],
    )
    decoded = _roundtrip(note)
    assert decoded.items == note.items
    assert decoded == note


def test_an_empty_checklist_is_not_mistaken_for_a_plain_note():
    # items=[] (a checklist with zero rows) must stay a checklist, not
    # collapse into items=None (a plain note) — they mean different things.
    note = _full_note(content=None, items=[])
    decoded = _roundtrip(note)
    assert decoded.items == []
    assert decoded.items is not None


def test_a_plain_note_has_no_item_list_at_all():
    note = _full_note(items=None)
    decoded = _roundtrip(note)
    assert decoded.items is None


@pytest.mark.parametrize("content", [None, "", "  ", "x"])
def test_content_none_vs_empty_string_are_distinct(content):
    note = _full_note(content=content, items=None, image_url=None)
    decoded = _roundtrip(note)
    assert decoded.content == content
    if content is None:
        assert decoded.content is None
    else:
        assert decoded.content is not None


@pytest.mark.parametrize("image_url", [None, "", "uploads/x.png"])
def test_image_url_none_vs_empty_string_are_distinct(image_url):
    note = _full_note(image_url=image_url)
    decoded = _roundtrip(note)
    assert decoded.image_url == image_url


def test_image_frontmatter_is_canonical_and_legacy_body_marker_still_reads():
    note = _full_note(content="body", image_url="uploads/new.png")
    text = note_to_markdown(note)
    assert "image: uploads/new.png" in text
    assert "odysseus-note:image" not in text
    assert markdown_to_note(text).image_url == "uploads/new.png"

    legacy = "---\nid: old\ntitle: Legacy\n---\nbody\n\n<!-- odysseus-note:image -->\n![](uploads/old.png)"
    assert markdown_to_note(legacy).image_url == "uploads/old.png"


@pytest.mark.parametrize(
    "title",
    [
        "",
        "Plain title",
        "Emoji party \U0001F389\U0001F60A",
        "Unicode: éèê résumé 中文",
        "A" * 500,  # very long title
        'Reserved chars < > : " * ? | \\ /',
        "../../etc/passwd",
        "....",
        "   leading and trailing spaces   ",
    ],
)
def test_title_round_trips_exactly_regardless_of_shape(title):
    # The filename derived from a title may be sanitized/truncated (see the
    # filename tests below), but the frontmatter copy of the title must
    # survive byte-for-byte — that's what makes the sanitization harmless.
    note = _full_note(title=title)
    decoded = _roundtrip(note)
    assert decoded.title == title


def test_content_containing_its_own_horizontal_rule_is_not_mistaken_for_frontmatter():
    content = "Intro paragraph.\n\n---\n\nMore text after a mid-note rule."
    note = _full_note(content=content, items=None, image_url=None)
    decoded = _roundtrip(note)
    assert decoded.content == content


def test_content_that_looks_like_a_task_list_is_not_confused_with_real_items():
    # The body can contain "- [ ] " lines as ordinary prose (e.g. a note ABOUT
    # markdown task lists). Because content is recovered by length, not by
    # searching for the items marker, this text is never misparsed as items.
    content = "Reminder: our syntax is\n- [ ] like this\n- [x] and this"
    note = _full_note(content=content, items=None, image_url=None)
    decoded = _roundtrip(note)
    assert decoded.content == content
    assert decoded.items is None


def test_content_containing_the_literal_items_marker_text_is_still_safe():
    content = "Someone pasted this literally: <!-- odysseus-note:items -->\nand kept typing."
    note = _full_note(content=content, items=[NoteItem(text="real item", done=False)], image_url=None)
    decoded = _roundtrip(note)
    assert decoded.content == content
    assert decoded.items == note.items


def test_external_body_append_is_not_truncated_by_a_legacy_length_hint():
    note = _full_note(content="Original body text.", items=None, image_url=None)
    text = note_to_markdown(note)
    # Older files in the wild have this hint.  It must be ignored after an
    # editor changes the body, otherwise the appended prose disappears.
    legacy = text.replace("title: Groceries\n", "title: Groceries\nx-content-len: 19\n")
    edited = legacy.replace("Original body text.", "Original body text. Added in Obsidian by hand.")
    decoded = markdown_to_note(edited)
    assert decoded.content == "Original body text. Added in Obsidian by hand."


def test_new_files_do_not_write_a_body_length_that_can_go_stale():
    text = note_to_markdown(_full_note(content="editable prose", items=None, image_url=None))
    assert "x-content-len:" not in text


def test_external_edit_before_metadata_block_preserves_the_full_body():
    note = _full_note(content="Original prose", items=[NoteItem(text="one")], image_url=None)
    text = note_to_markdown(note)
    edited = text.replace("Original prose", "Original prose\n\nA paragraph typed in Obsidian")
    decoded = markdown_to_note(edited)
    assert decoded.content == "Original prose\n\nA paragraph typed in Obsidian"
    assert decoded.items == [NoteItem(text="one")]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no special chars",
        "line one\nline two\nline three",
        "trailing backslash\\",
        "literal backslash-n: \\n (not a real newline)",
        "unicode ☃ and emoji \U0001F600",
        "a" * 300,
    ],
)
def test_item_text_round_trips_including_newlines_and_backslashes(text):
    note = _full_note(content=None, items=[NoteItem(text=text, done=True)], image_url=None)
    decoded = _roundtrip(note)
    assert decoded.items[0].text == text
    assert decoded.items[0].done is True


def test_item_extra_keys_beyond_text_and_done_are_preserved():
    note = _full_note(
        content=None,
        items=[NoteItem(text="step 1", done=False, extra={"due": "2026-01-01", "id": "sub-1"})],
        image_url=None,
    )
    decoded = _roundtrip(note)
    assert decoded.items[0].extra == {"due": "2026-01-01", "id": "sub-1"}


def test_label_none_vs_empty_string_are_distinct():
    for label in (None, "", "work"):
        note = _full_note(label=label)
        decoded = _roundtrip(note)
        assert decoded.label == label


def test_due_date_and_repeat_and_sort_order_and_pinned_round_trip():
    note = _full_note(due_date="2026-12-25", repeat="daily", sort_order=42, pinned=False)
    decoded = _roundtrip(note)
    assert decoded.due_date == "2026-12-25"
    assert decoded.repeat == "daily"
    assert decoded.sort_order == 42
    assert decoded.pinned is False


def test_ai_fields_and_agent_session_and_source_and_session_round_trip():
    note = _full_note(
        ai_classification='{"kind": "task"}',
        ai_content_hash="deadbeefcafe",
        agent_session_id="agent-42",
        source="agent",
        session_id="chat-99",
    )
    decoded = _roundtrip(note)
    assert decoded.ai_classification == '{"kind": "task"}'
    assert decoded.ai_content_hash == "deadbeefcafe"
    assert decoded.agent_session_id == "agent-42"
    assert decoded.source == "agent"
    assert decoded.session_id == "chat-99"


def test_created_and_updated_timestamps_round_trip_to_the_second():
    note = _full_note(
        created_at=datetime(2026, 6, 15, 3, 4, 5),
        updated_at=datetime(2026, 6, 16, 23, 59, 59),
    )
    decoded = _roundtrip(note)
    assert decoded.created_at == note.created_at
    assert decoded.updated_at == note.updated_at


def test_missing_timestamps_decode_to_none():
    note = _full_note(created_at=None, updated_at=None)
    decoded = _roundtrip(note)
    assert decoded.created_at is None
    assert decoded.updated_at is None


def test_owner_none_vs_empty_string_are_distinct():
    for owner in (None, "", "rob"):
        note = _full_note(owner=owner)
        decoded = _roundtrip(note)
        assert decoded.owner == owner


def test_id_is_preserved_as_the_notes_real_identity():
    note = _full_note(id="some-uuid-1234")
    text = note_to_markdown(note)
    assert "some-uuid-1234" in text
    decoded = markdown_to_note(text)
    assert decoded.id == "some-uuid-1234"


def test_a_file_with_no_id_in_frontmatter_is_adopted_with_a_fresh_one():
    text = "---\ntitle: Hand-written note\n---\nJust some prose.\n"
    decoded = markdown_to_note(text)
    assert decoded.id  # a uuid was minted, not left blank/None
    assert decoded.title == "Hand-written note"


# ---------------------------------------------------------------------------
# note_record_from_orm — the ORM-facing adapter
# ---------------------------------------------------------------------------


def test_note_record_from_orm_reads_a_plain_note():
    row = SimpleNamespace(
        id="n1", owner="rob", title="T", content="body", items=None, note_type="note",
        color=None, label=None, pinned=False, archived=False, due_date=None,
        source="user", session_id=None, sort_order=0, image_url=None, repeat="none",
        ai_classification=None, ai_content_hash=None, agent_session_id=None,
        created_at=None, updated_at=None,
    )
    record = note_record_from_orm(row)
    assert record.items is None
    assert record.title == "T"


def test_note_record_from_orm_treats_an_empty_checklist_as_a_checklist():
    # A checklist row whose items JSON is empty/absent must still decode as
    # items=[] (checklist), not items=None (plain note) — note_type carries
    # that information on the ORM side even though our own codec derives it
    # the other way around when writing to Markdown.
    row = SimpleNamespace(
        id="n2", owner="rob", title="T", content=None, items=None, note_type="checklist",
        color=None, label=None, pinned=False, archived=False, due_date=None,
        source="user", session_id=None, sort_order=0, image_url=None, repeat="none",
        ai_classification=None, ai_content_hash=None, agent_session_id=None,
        created_at=None, updated_at=None,
    )
    record = note_record_from_orm(row)
    assert record.items == []


def test_note_record_from_orm_parses_items_json_with_extra_keys():
    row = SimpleNamespace(
        id="n3", owner=None, title="T", content=None,
        items='[{"text": "a", "done": true, "note_id": "sub"}]',
        note_type="checklist",
        color=None, label=None, pinned=False, archived=False, due_date=None,
        source="user", session_id=None, sort_order=0, image_url=None, repeat="none",
        ai_classification=None, ai_content_hash=None, agent_session_id=None,
        created_at=None, updated_at=None,
    )
    record = note_record_from_orm(row)
    assert record.items[0].text == "a"
    assert record.items[0].done is True
    assert record.items[0].extra == {"note_id": "sub"}


# ---------------------------------------------------------------------------
# Filenames: safe, deterministic, collision-resistant
# ---------------------------------------------------------------------------


def test_slugify_strips_path_separators_so_a_title_cannot_become_a_path():
    slug = slugify_title("../../etc/passwd")
    assert "/" not in slug
    assert "\\" not in slug


def test_slugify_handles_every_windows_unsafe_character():
    slug = slugify_title('a<b>c:d"e/f\\g|h?i*j')
    assert not set('<>:"/\\|?*') & set(slug)


def test_slugify_of_pure_punctuation_or_whitespace_is_empty():
    for title in ("", "   ", "...", "///", "***"):
        assert slugify_title(title) == ""


def test_slugify_truncates_very_long_titles():
    slug = slugify_title("x" * 500)
    assert len(slug) <= 80


def test_slugify_escapes_windows_reserved_device_names():
    for reserved in ("CON", "con", "PRN", "COM1", "LPT9", "NUL"):
        slug = slugify_title(reserved)
        assert slug.lower() not in {"con", "prn", "aux", "nul", "com1", "lpt9"}


def test_note_filename_falls_back_to_id_when_title_has_no_usable_characters():
    name = note_filename("../../..", "abcdef1234", set())
    assert name.endswith(".md")
    assert "/" not in name and "\\" not in name


def test_note_filename_is_deterministic_for_the_same_inputs():
    a = note_filename("Groceries", "n1", {"groceries"})
    b = note_filename("Groceries", "n1", {"groceries"})
    assert a == b


def test_note_filename_resolves_collisions_with_a_stable_numeric_suffix():
    first = note_filename("Groceries", "n1", set())
    second = note_filename("Groceries", "n2", {first[:-3].lower()})
    third = note_filename("Groceries", "n3", {first[:-3].lower(), second[:-3].lower()})
    assert first != second != third
    assert len({first, second, third}) == 3


def test_resolve_note_directory_uses_archived_flag_only():
    assert resolve_note_directory(False, "Notes", "Notes/Archive") == "Notes"
    assert resolve_note_directory(True, "Notes", "Notes/Archive") == "Notes/Archive"


def test_safe_join_refuses_to_escape_the_vault_root(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    assert safe_join(vault, "Notes/ok.md") == (vault / "Notes" / "ok.md").resolve()
    assert safe_join(vault, "../outside.md") is None
    assert safe_join(vault, "Notes/../../outside.md") is None


def test_safe_join_refuses_an_absolute_path_escape(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    # An absolute path handed to Path.__truediv__ replaces the base entirely,
    # so this must still resolve outside vault_root and be refused.
    assert safe_join(vault, str(outside)) is None
