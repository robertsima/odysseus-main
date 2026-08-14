"""Obsidian structure has to survive indexing.

A vault records what a note *is* in its frontmatter, where a passage belongs in
its headings, and which notes belong together in its ``[[wikilinks]]``. The
generic text indexer saw none of that: frontmatter went in as ``---`` noise,
headings were discarded by sentence splitting, and links were punctuation.
These pin the parsing and chunking that put it back.
"""
import os
import time
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

from src.vault_markdown import (
    MarkdownDoc,
    build_chunk_header,
    chunk_markdown,
    date_from_filename,
    decode_list,
    encode_list,
    extract_inline_tags,
    extract_wikilinks,
    format_date,
    list_contains,
    note_key,
    parse_date_value,
    parse_markdown,
    split_frontmatter,
    strip_chunk_header,
)


def _split(text, chunk_size=1000):
    """Stand-in for VectorRAG._split_into_chunks: hard character split."""
    if len(text) <= chunk_size:
        return [text]
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]


# -- frontmatter -----------------------------------------------------------


def test_frontmatter_is_removed_from_the_body():
    fm, body = split_frontmatter("---\ntags: [a, b]\n---\nActual prose.\n")
    assert fm == {"tags": ["a", "b"]}
    assert body.strip() == "Actual prose."
    assert "---" not in body


def test_a_note_without_frontmatter_is_returned_whole():
    text = "# Heading\n\nSome prose.\n"
    assert split_frontmatter(text) == ({}, text)


def test_a_horizontal_rule_mid_note_is_not_frontmatter():
    # The delimiter only counts at the very start of the file; otherwise every
    # note using --- as a section break would lose its opening paragraph.
    text = "Opening line.\n\n---\n\nMore prose.\n"
    assert split_frontmatter(text) == ({}, text)


def test_unparseable_frontmatter_still_yields_the_body():
    # A broken header must cost the header, never the note.
    fm, body = split_frontmatter("---\ntags: [unclosed\n  : :\n---\nProse survives.\n")
    assert fm == {}
    assert "Prose survives." in body


@pytest.mark.parametrize("header", [
    "tags: [project, ai]",
    "tags: project, ai",
    "tags:\n  - project\n  - ai",
    "tags:\n  - '#project'\n  - '#ai'",
])
def test_every_obsidian_tag_syntax_parses(header):
    doc = parse_markdown(f"---\n{header}\n---\nbody\n", "n.md")
    assert doc.tags == ["project", "ai"]


def test_aliases_are_captured_and_lowercased():
    doc = parse_markdown("---\naliases: [AI Mind, Vault Mind]\n---\nbody\n", "n.md")
    assert doc.aliases == ["ai mind", "vault mind"]


# -- inline tags -----------------------------------------------------------


def test_inline_tags_are_collected():
    assert extract_inline_tags("Working on #project/odysseus today #ai") == [
        "project/odysseus", "ai",
    ]


def test_headings_are_not_tags():
    assert extract_inline_tags("# Heading\n## Another") == []


def test_tags_inside_code_fences_are_ignored():
    # A shell comment is not a vault tag, and a note about shell scripting
    # would otherwise be tagged with every comment in every example.
    text = "Real #tag here.\n\n```bash\n# not-a-tag\necho '#alsonot'\n```\n"
    assert extract_inline_tags(text) == ["tag"]


def test_bare_numbers_are_not_tags():
    assert extract_inline_tags("see issue #5559 from #2026") == []


def test_a_url_fragment_is_not_a_tag():
    assert extract_inline_tags("https://example.com/docs#section") == []


# -- wikilinks -------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("[[Sarah]]", "sarah"),
    ("[[Sarah|her]]", "sarah"),
    ("[[Sarah#Allergies]]", "sarah"),
    ("[[people/Sarah]]", "sarah"),
    ("[[Retention Policy]]", "retention policy"),
])
def test_wikilink_forms_resolve_to_a_note_key(raw, expected):
    assert extract_wikilinks(f"see {raw} for detail") == [expected]


def test_wikilinks_are_deduplicated_in_document_order():
    assert extract_wikilinks("[[b]] [[a]] [[B]]") == ["b", "a"]


def test_note_key_strips_only_markdown_extensions():
    assert note_key("Some Note.md") == "some note"
    assert note_key("data.v2.json") == "data.v2.json"


# -- dates -----------------------------------------------------------------


def _epoch(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("name,expected", [
    ("2026-08-14.md", _epoch(2026, 8, 14)),
    ("2026_08_14 journal.md", _epoch(2026, 8, 14)),
    ("20260814.md", _epoch(2026, 8, 14)),
    ("25-12-2026.md", _epoch(2026, 12, 25)),   # 25 > 12, so day-first
    ("12-25-2026.md", _epoch(2026, 12, 25)),   # 25 > 12, so month-first
])
def test_dates_are_recovered_from_filenames(name, expected):
    assert date_from_filename(name) == expected


def test_an_ambiguous_filename_date_follows_the_configured_order(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_VAULT_DATE_ORDER", "month")
    assert date_from_filename("03-04-2026.md") == _epoch(2026, 3, 4)
    monkeypatch.setenv("ODYSSEUS_VAULT_DATE_ORDER", "day")
    assert date_from_filename("03-04-2026.md") == _epoch(2026, 4, 3)


def test_a_non_date_filename_yields_nothing():
    assert date_from_filename("Odysseus Reference.md") is None


def test_frontmatter_date_beats_the_filename():
    doc = parse_markdown(
        "---\nupdated: 2026-08-14\n---\nbody\n", "2020-01-01.md"
    )
    assert doc.doc_date == _epoch(2026, 8, 14)
    assert doc.doc_date_source == "frontmatter"


def test_updated_beats_created_when_both_are_present():
    # What matters for conflict resolution is when the note was last asserted,
    # not when it was born.
    doc = parse_markdown(
        "---\ncreated: 2020-01-01\nupdated: 2026-08-14\n---\nbody\n", "n.md"
    )
    assert doc.doc_date == _epoch(2026, 8, 14)


def test_mtime_is_the_last_resort_and_is_labelled_as_such():
    now = time.time()
    doc = parse_markdown("plain note", "Notes.md", mtime=now)
    assert doc.doc_date == now
    assert doc.doc_date_source == "mtime"


def test_a_note_with_no_date_signal_at_all_has_none():
    doc = parse_markdown("plain note", "Notes.md")
    assert doc.doc_date is None
    assert doc.doc_date_source == ""


@pytest.mark.parametrize("value", [None, "", "not a date", 42, [1, 2]])
def test_unparseable_date_values_are_none(value):
    assert parse_date_value(value) is None


def test_format_date_is_empty_for_unknown():
    assert format_date(None) == ""
    assert format_date(0) == ""
    assert format_date(_epoch(2026, 8, 14)) == "2026-08-14"


# -- title -----------------------------------------------------------------


def test_title_prefers_frontmatter_then_first_heading_then_filename():
    assert parse_markdown("---\ntitle: Explicit\n---\n# Heading\n", "f.md").title == "Explicit"
    assert parse_markdown("# Heading\n", "f.md").title == "Heading"
    assert parse_markdown("no heading\n", "Fallback Name.md").title == "Fallback Name"


# -- list encoding ---------------------------------------------------------


def test_encode_decode_roundtrip():
    assert decode_list(encode_list(["A", "b", "A"])) == ["a", "b"]


def test_empty_list_encodes_to_empty_string():
    assert encode_list([]) == ""
    assert decode_list("") == []
    assert decode_list(None) == []


def test_membership_cannot_match_a_longer_neighbouring_value():
    # Without the sentinel delimiters, "|ai|" would substring-match "|ai mind|"
    # and every note tagged for the vault would answer to the tag "ai".
    packed = encode_list(["ai mind", "project"])
    assert list_contains(packed, "ai mind") is True
    assert list_contains(packed, "ai") is False


def test_a_value_containing_the_separator_is_sanitised():
    assert decode_list(encode_list(["a|b"])) == ["a/b"]


# -- heading-aware chunking ------------------------------------------------

NOTE = """# Odysseus

Intro paragraph.

## Deployment

Runs on the NAS.

### Volumes

The vault is mounted read-only.

## Retrieval

Uses ChromaDB.
"""


def test_each_chunk_records_where_in_the_outline_it_starts():
    doc = parse_markdown(NOTE, "Odysseus.md")
    chunks = chunk_markdown(doc, _split, chunk_size=60)
    paths = [c.heading_path for c in chunks]
    assert "Odysseus > Deployment" in paths
    assert "Odysseus > Deployment > Volumes" in paths
    assert "Odysseus > Retrieval" in paths


def test_heading_text_stays_in_the_chunk_body():
    # The heading words are usually the ones a query would use; dropping them
    # to metadata alone makes them unmatchable by the keyword score.
    doc = parse_markdown(NOTE, "Odysseus.md")
    chunks = chunk_markdown(doc, _split, chunk_size=60)
    deployment = next(c for c in chunks if c.heading_path == "Odysseus > Deployment")
    assert "## Deployment" in deployment.text


def test_small_sections_are_packed_rather_than_exploded():
    # A note of one-line headings must not become one chunk per line.
    doc = parse_markdown(NOTE, "Odysseus.md")
    assert len(chunk_markdown(doc, _split, chunk_size=1000)) == 1


def test_a_section_larger_than_the_chunk_size_is_split_by_the_caller():
    body = "# Big\n\n" + ("word " * 400)
    chunks = chunk_markdown(parse_markdown(body, "Big.md"), _split, chunk_size=200)
    assert len(chunks) > 1
    assert all(c.heading_path == "Big" for c in chunks)


def test_headings_inside_code_fences_do_not_start_a_section():
    body = "# Real\n\n```python\n# not a heading\nx = 1\n```\n\nprose\n"
    chunks = chunk_markdown(parse_markdown(body, "n.md"), _split, chunk_size=1000)
    assert {c.heading_path for c in chunks} == {"Real"}


def test_a_note_with_no_headings_still_chunks():
    doc = parse_markdown("just prose, no structure at all", "n.md")
    chunks = chunk_markdown(doc, _split, chunk_size=1000)
    assert len(chunks) == 1
    assert chunks[0].heading_path == ""


def test_an_empty_note_produces_no_chunks():
    assert chunk_markdown(parse_markdown("---\ntags: [a]\n---\n", "n.md"), _split) == []


def test_a_deeper_heading_after_a_skipped_level_does_not_crash():
    # Real vaults skip levels (h1 straight to h3). The breadcrumb should hold
    # the levels that exist and nothing else.
    doc = parse_markdown("# One\n\n### Three\n\nbody\n", "n.md")
    paths = [c.heading_path for c in chunk_markdown(doc, _split, chunk_size=20)]
    assert paths[-1] == "One > Three"


# -- chunk header ----------------------------------------------------------


def test_the_header_carries_the_signals_that_must_be_searchable():
    header = build_chunk_header(
        "08-08-2026.md",
        heading_path="Journal > Morning",
        tags=["daily"],
        aliases=["today"],
        doc_date=_epoch(2026, 8, 8),
    )
    # The bare stem has to be its own token — search() tokenises on whitespace,
    # so "08-08-2026.md" alone would not match a query saying "08-08-2026".
    assert "08-08-2026" in header.split()
    assert "Section: Journal > Morning" in header
    assert "#daily" in header
    assert "Updated: 2026-08-08" in header


def test_the_header_omits_fields_it_has_nothing_for():
    assert build_chunk_header("LICENSE") == "Source: LICENSE"


def test_strip_removes_exactly_the_header_it_built():
    header = build_chunk_header("n.md", heading_path="A > B", tags=["x"], doc_date=_epoch(2026, 1, 1))
    body = "## B\n\nThe actual prose."
    assert strip_chunk_header(f"{header}\n{body}") == body


def test_strip_leaves_a_pre_existing_chunk_untouched():
    # Chunks written before headers existed must render unchanged.
    assert strip_chunk_header("Just some prose.") == "Just some prose."


def test_strip_does_not_eat_prose_that_merely_looks_like_a_field():
    assert strip_chunk_header("Sources: three of them\nmore") == "Sources: three of them\nmore"


# -- end-to-end parse ------------------------------------------------------


def test_a_realistic_note_yields_every_signal():
    text = (
        "---\n"
        "tags: [project, ai]\n"
        "aliases: [AI Mind]\n"
        "updated: 2026-08-14\n"
        "---\n"
        "# Vault Mind\n\n"
        "Linked to [[Odysseus Reference]] and #retrieval work.\n"
    )
    doc = parse_markdown(text, "Vault Mind.md")
    assert isinstance(doc, MarkdownDoc)
    assert doc.title == "Vault Mind"
    assert doc.key == "vault mind"
    assert doc.tags == ["project", "ai", "retrieval"]
    assert doc.aliases == ["ai mind"]
    assert doc.links == ["odysseus reference"]
    assert doc.doc_date == _epoch(2026, 8, 14)
    assert "tags:" not in doc.body
