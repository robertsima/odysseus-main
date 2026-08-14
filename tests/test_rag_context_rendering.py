"""What retrieval hands the model, and what it costs.

Retrieval can rank perfectly and still produce a wrong answer if the block it
builds hides the evidence. Two failures matter here: a long note truncating
every other source out of the block, and undated snippets that leave the model
choosing arbitrarily between contradictory notes.
"""
import os
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

from src.chat_processor import (
    RAG_CONFLICT_POLICY,
    _rag_source_entry,
    render_retrieved_documents,
)
from src.vault_markdown import build_chunk_header, describe_chunk, encode_list


def _epoch(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc).timestamp()


def _result(filename, body, *, doc_date=None, source_kind="frontmatter",
            tags=(), heading="", similarity=0.8, path="direct"):
    return {
        "document": body,
        "similarity": similarity,
        "retrieval_path": path,
        "metadata": {
            "filename": filename,
            "source": f"/vault/{filename}",
            "doc_date": doc_date or 0.0,
            "doc_date_source": source_kind if doc_date else "",
            "tags": encode_list(list(tags)),
            "heading_path": heading,
        },
    }


# -- labels ----------------------------------------------------------------


def test_the_label_carries_the_date_the_note_was_last_true():
    label = describe_chunk(_result("Deploy.md", "x", doc_date=_epoch(2026, 8, 14))["metadata"])
    assert "Deploy.md" in label
    assert "updated 2026-08-14" in label


def test_a_date_taken_from_the_filename_is_labelled_differently():
    # "dated" rather than "updated": a journal named for its day says when it
    # was written about, not when it was last revised.
    label = describe_chunk(
        _result("08-08-2026.md", "x", doc_date=_epoch(2026, 8, 8), source_kind="filename")["metadata"]
    )
    assert "dated 2026-08-08" in label


def test_an_undated_note_gets_no_date_claim():
    label = describe_chunk(_result("Notes.md", "x")["metadata"])
    assert "Notes.md" in label
    assert "updated" not in label and "dated" not in label


def test_the_label_names_the_section_and_tags():
    label = describe_chunk(
        _result("Odysseus.md", "x", heading="Deployment > NAS", tags=["project"])["metadata"]
    )
    assert "section: Deployment > NAS" in label
    assert "#project" in label


@pytest.mark.parametrize("meta", [None, "nope", {}])
def test_a_malformed_or_bare_chunk_still_renders(meta):
    assert describe_chunk(meta)


# -- the block -------------------------------------------------------------


def test_the_block_tells_the_model_how_to_resolve_a_conflict():
    # Dates in the labels are useless unless the model is told what to do with
    # them: without this it reads two contradictory notes as one contradictory
    # corpus and answers from whichever it saw first.
    block = render_retrieved_documents([_result("A.md", "content", doc_date=_epoch(2026, 1, 1))])
    assert RAG_CONFLICT_POLICY in block


def test_a_long_note_cannot_truncate_the_other_sources_away():
    # The previous behaviour sliced the concatenated block at a fixed length,
    # so a single 40k-character note consumed the budget and every source after
    # it vanished with no signal.
    results = [
        _result("Long.md", "L" * 40000),
        _result("Short.md", "the decisive detail"),
        _result("Third.md", "another source entirely"),
    ]
    block = render_retrieved_documents(results, budget=6000)

    assert "the decisive detail" in block
    assert "another source entirely" in block
    assert len(block) <= 6000


def test_truncation_is_visible_when_it_happens():
    block = render_retrieved_documents([_result("Long.md", "L" * 40000)], budget=2000)
    assert "[…truncated]" in block


def test_short_sources_are_never_truncated_to_make_room():
    # Unused share is redistributed, so three short notes all arrive whole.
    results = [_result(f"{i}.md", f"body {i}") for i in range(3)]
    block = render_retrieved_documents(results, budget=6000)
    for i in range(3):
        assert f"body {i}" in block
    assert "truncated" not in block


def test_the_embedded_provenance_header_is_not_repeated_in_the_prompt():
    # The header has to be in the indexed text for retrieval to score against
    # it, but rendering it verbatim as well pays for the same facts twice.
    header = build_chunk_header("Deploy.md", heading_path="NAS", tags=["ops"],
                                doc_date=_epoch(2026, 8, 14))
    block = render_retrieved_documents([
        _result("Deploy.md", f"{header}\nThe actual prose.", doc_date=_epoch(2026, 8, 14),
                heading="NAS", tags=["ops"])
    ])

    assert block.count("Deploy.md") == 1
    assert "Source: Deploy.md" not in block
    assert "The actual prose." in block


def test_an_empty_result_set_renders_nothing():
    assert render_retrieved_documents([]) == ""


def test_every_source_is_labelled_in_order():
    block = render_retrieved_documents([_result("A.md", "a"), _result("B.md", "b")])
    assert block.index("A.md") < block.index("B.md")


# -- the sources list shown in the UI --------------------------------------


def test_a_source_entry_reports_the_date_and_section():
    entry = _rag_source_entry(
        _result("Deploy.md", "body", doc_date=_epoch(2026, 8, 14), heading="NAS")
    )
    assert entry["filename"] == "Deploy.md"
    assert entry["updated"] == "2026-08-14"
    assert entry["section"] == "NAS"


def test_a_source_reached_by_a_link_is_marked_as_such():
    entry = _rag_source_entry(_result("Sarah.md", "body", path="link"))
    assert entry["via"] == "link"
    assert "via" not in _rag_source_entry(_result("Direct.md", "body"))


def test_the_snippet_excludes_the_provenance_header():
    header = build_chunk_header("Deploy.md", heading_path="NAS")
    entry = _rag_source_entry(_result("Deploy.md", f"{header}\nReal content here."))
    assert entry["snippet"].startswith("Real content here.")


def test_a_legacy_result_without_vault_metadata_still_renders():
    legacy = {"document": "old chunk", "similarity": 0.5,
              "metadata": {"filename": "old.md", "source": "/v/old.md"}}
    entry = _rag_source_entry(legacy)
    assert entry["filename"] == "old.md"
    assert "updated" not in entry
    assert "old chunk" in render_retrieved_documents([legacy])


# -- the agent's search_documents tool -------------------------------------


def test_the_agent_document_search_reports_dates_and_drops_the_header():
    # The agent reasoning over the vault needs the same temporal signal the
    # chat path gets, or it answers from a superseded note with no way to know.
    from src.personal_docs import retrieve_personal

    header = build_chunk_header("Deploy.md", heading_path="NAS", doc_date=_epoch(2026, 8, 14))

    class _Rag:
        def search(self, query, k, allow_private=True):
            return [_result("Deploy.md", f"{header}\nDeploy from the NAS.",
                            doc_date=_epoch(2026, 8, 14), heading="NAS")]

    out = retrieve_personal([], "how do we deploy", k=3, rag_manager=_Rag())

    assert len(out) == 1
    assert "Deploy.md" in out[0]
    assert "updated 2026-08-14" in out[0]
    assert "Deploy from the NAS." in out[0]
    assert "Source: Deploy.md" not in out[0]


def test_the_agent_document_search_still_works_on_a_legacy_chunk():
    from src.personal_docs import retrieve_personal

    class _Rag:
        def search(self, query, k, allow_private=True):
            return [{"document": "old chunk", "metadata": {"source": "/vault/old.md"}}]

    out = retrieve_personal([], "anything", k=3, rag_manager=_Rag())

    assert "old.md" in out[0]
    assert "old chunk" in out[0]
