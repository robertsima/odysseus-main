"""Ranking signals a similarity score cannot express.

Temporal validity, tag/alias vocabulary, and source diversity. Each is a way a
knowledge base differs from a document pile: it accumulates over time, it is
organised deliberately, and it repeats itself. These pin the signal functions;
test_rag_vault_retrieval.py pins their effect on ordering.
"""
import os
import time

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

from src.rag_ranking import (
    NEUTRAL,
    cap_per_document,
    collect_link_targets,
    link_expansion_enabled,
    max_chunks_per_document,
    query_has_temporal_intent,
    query_tag_tokens,
    recency_factor,
    result_note_keys,
    tag_alias_score,
    temporal_factor,
    temporal_multiplier,
)
from src.vault_markdown import encode_list

DAY = 86400.0
NOW = 1_760_000_000.0


def _ago(days):
    return NOW - days * DAY


# -- temporal intent -------------------------------------------------------


@pytest.mark.parametrize("query", [
    "what is my current deploy process",
    "what's the latest on the NAS",
    "has this changed recently",
    "as of now, what does the policy say",
    "is that still true",
    "what did we agree this week",
])
def test_queries_about_the_present_are_detected(query):
    assert query_has_temporal_intent(query) is True


@pytest.mark.parametrize("query", [
    "how does chromadb store embeddings",
    "explain the retention policy",
    "what did I write in my journal on 08-08-2026",
    "",
])
def test_ordinary_queries_are_not_temporal(query):
    # A false positive here silently down-ranks every older note for a question
    # that never asked about time, so the detector has to stay narrow.
    assert query_has_temporal_intent(query) is False


# -- recency ---------------------------------------------------------------


def test_a_newer_note_scores_higher_than_an_older_one():
    fresh = recency_factor(_ago(1), "frontmatter", now=NOW)
    stale = recency_factor(_ago(1000), "frontmatter", now=NOW)
    assert fresh > stale


def test_an_unknown_date_is_exactly_neutral():
    # An index built before dates were extracted must rank as it always did.
    assert recency_factor(None, "", now=NOW) == NEUTRAL
    assert recency_factor(0, "", now=NOW) == NEUTRAL


def test_an_mtime_date_moves_the_score_less_than_a_frontmatter_one():
    # A filesystem timestamp moves on re-sync and restore-from-backup, neither
    # of which means the content became newly true.
    trusted = recency_factor(_ago(2), "frontmatter", now=NOW)
    guessed = recency_factor(_ago(2), "mtime", now=NOW)
    assert trusted > guessed > NEUTRAL


def test_a_future_dated_note_is_treated_as_current():
    # Scheduled entries and journals written ahead are common in vaults.
    assert recency_factor(NOW + 30 * DAY, "frontmatter", now=NOW) == pytest.approx(1.0)


def test_the_half_life_is_configurable(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_RAG_RECENCY_HALFLIFE_DAYS", "10")
    fast = recency_factor(_ago(100), "frontmatter", now=NOW)
    monkeypatch.setenv("ODYSSEUS_RAG_RECENCY_HALFLIFE_DAYS", "3650")
    slow = recency_factor(_ago(100), "frontmatter", now=NOW)
    assert slow > fast


@pytest.mark.parametrize("raw", ["nonsense", "-5", "999999999"])
def test_a_bad_half_life_falls_back_to_the_default(monkeypatch, raw):
    monkeypatch.setenv("ODYSSEUS_RAG_RECENCY_HALFLIFE_DAYS", raw)
    assert recency_factor(_ago(10), "frontmatter", now=NOW) == pytest.approx(
        recency_factor(_ago(10), "frontmatter", now=NOW)
    )
    assert 0.0 <= recency_factor(_ago(10), "frontmatter", now=NOW) <= 1.0


# -- multiplier bounds -----------------------------------------------------


def test_a_non_temporal_query_barely_moves_the_score():
    # The default bias is a tie-break, not a re-ranking: an old note is not a
    # wrong note.
    assert temporal_multiplier(0.0, intent=False) >= 0.94
    assert temporal_multiplier(1.0, intent=False) == pytest.approx(1.0)


def test_a_temporal_query_moves_it_meaningfully_but_stays_bounded():
    low = temporal_multiplier(0.0, intent=True)
    high = temporal_multiplier(1.0, intent=True)
    assert 0.6 <= low <= 0.8
    assert high == pytest.approx(1.0)


def test_the_multiplier_never_exceeds_one():
    # Ranking must only ever discount, so a fresh chunk cannot be pushed past
    # the name-match floor by age alone.
    for recency in (0.0, 0.5, 1.0, 5.0, -3.0):
        assert temporal_multiplier(recency, intent=True) <= 1.0


def test_metadata_without_a_date_lands_at_the_neutral_factor():
    assert temporal_factor({"filename": "n.md"}, intent=True) == pytest.approx(
        temporal_multiplier(NEUTRAL, True)
    )


@pytest.mark.parametrize("meta", [None, "nope", 5])
def test_malformed_metadata_earns_no_adjustment(meta):
    assert temporal_factor(meta, intent=True) == 1.0


def test_the_bias_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_RAG_TEMPORAL_WEIGHT", "0")
    monkeypatch.setenv("ODYSSEUS_RAG_TEMPORAL_INTENT_WEIGHT", "0")
    meta = {"doc_date": _ago(3000), "doc_date_source": "frontmatter"}
    assert temporal_factor(meta, intent=True) == pytest.approx(1.0)


# -- tags and aliases ------------------------------------------------------


def test_an_explicit_hash_tag_in_the_query_is_extracted():
    assert query_tag_tokens("show me #project/odysseus and #ai notes") == {
        "project/odysseus", "ai",
    }


def test_an_explicit_tag_match_earns_full_credit():
    meta = {"tags": encode_list(["project"])}
    assert tag_alias_score("#project notes", {"notes"}, meta) == 1.0


def test_a_parent_tag_matches_a_nested_one():
    # Nesting exists precisely so the parent can be used as a filter.
    meta = {"tags": encode_list(["project/odysseus"])}
    assert tag_alias_score("#project", set(), meta) == 1.0


def test_a_bare_word_matching_a_tag_earns_partial_credit():
    meta = {"tags": encode_list(["retrieval"])}
    score = tag_alias_score("how does retrieval work", {"how", "does", "retrieval", "work"}, meta)
    assert 0 < score < 1.0


def test_a_multi_word_alias_matches_against_the_raw_query():
    # Token-set matching alone would never fire for "ai mind".
    meta = {"aliases": encode_list(["ai mind"])}
    assert tag_alias_score("what is in my ai mind setup", {"ai", "mind", "setup"}, meta) > 0


def test_a_chunk_with_no_tags_scores_nothing():
    assert tag_alias_score("#project", {"project"}, {"filename": "n.md"}) == 0.0


@pytest.mark.parametrize("meta", [None, "nope", {}])
def test_tag_scoring_tolerates_missing_metadata(meta):
    assert tag_alias_score("#x", {"x"}, meta) == 0.0


# -- link graph ------------------------------------------------------------


def _row(note, links=(), score=1.0):
    return {
        "id": note,
        "similarity": score,
        "metadata": {
            "note_key": note,
            "source": f"/v/{note}.md",
            "filename": f"{note}.md",
            "links": encode_list(list(links)),
        },
    }


def test_links_are_collected_from_the_top_results_only():
    rows = [_row("a", ["x"]), _row("b", ["y"]), _row("c", ["z"]), _row("d", ["w"])]
    assert collect_link_targets(rows, depth_limit=2) == ["x", "y"]


def test_notes_already_retrieved_are_not_re_fetched():
    rows = [_row("a", ["b"]), _row("b", ["c"])]
    targets = collect_link_targets(rows, depth_limit=5, exclude=result_note_keys(rows))
    assert targets == ["c"]


def test_an_index_without_link_metadata_yields_no_targets():
    assert collect_link_targets([{"metadata": {"filename": "n.md"}}], depth_limit=3) == []


def test_expansion_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_RAG_LINK_EXPANSION", "0")
    assert link_expansion_enabled() is False
    monkeypatch.setenv("ODYSSEUS_RAG_LINK_EXPANSION", "1")
    assert link_expansion_enabled() is True


def test_expansion_is_on_by_default(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_RAG_LINK_EXPANSION", raising=False)
    assert link_expansion_enabled() is True


# -- diversity cap ---------------------------------------------------------


def _chunk(source, score):
    return {"similarity": score, "metadata": {"source": source, "filename": source}}


def test_a_weaker_second_source_still_gets_in():
    # Every chunk of long.md outscores other.md, so a plain top-k returns one
    # source and the model never sees that a second one exists — which is
    # exactly how a contradiction stays invisible.
    rows = [_chunk("/v/long.md", 0.9 - i * 0.01) for i in range(5)]
    rows.append(_chunk("/v/other.md", 0.5))
    kept = cap_per_document(rows, limit=5, max_per_doc=2)
    sources = [r["metadata"]["source"] for r in kept]
    assert "/v/other.md" in sources
    # And it arrives ahead of the chunks the cap held back, not appended after.
    assert sources.index("/v/other.md") == 2


def test_the_cap_binds_when_there_are_enough_sources_to_fill_the_slots():
    rows = [_chunk("/v/long.md", 0.9 - i * 0.01) for i in range(5)]
    rows += [_chunk(f"/v/other{i}.md", 0.5) for i in range(3)]
    kept = cap_per_document(rows, limit=5, max_per_doc=2)
    sources = [r["metadata"]["source"] for r in kept]
    assert sources.count("/v/long.md") == 2
    assert len(set(sources)) == 4


def test_held_back_chunks_backfill_when_there_is_nothing_else():
    # A vault where the answer genuinely lives in one long note must not be
    # returned half-empty.
    rows = [_chunk("/v/long.md", 0.9 - i * 0.01) for i in range(5)]
    kept = cap_per_document(rows, limit=5, max_per_doc=2)
    assert len(kept) == 5


def test_backfill_preserves_score_order():
    rows = [_chunk("/v/a.md", 0.9), _chunk("/v/a.md", 0.8), _chunk("/v/a.md", 0.7)]
    kept = cap_per_document(rows, limit=3, max_per_doc=1)
    assert [r["similarity"] for r in kept] == [0.9, 0.8, 0.7]


def test_the_cap_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_RAG_MAX_CHUNKS_PER_DOC", "0")
    assert max_chunks_per_document() == 0
    rows = [_chunk("/v/long.md", 0.9) for _ in range(4)]
    assert len(cap_per_document(rows, limit=4)) == 4


def test_chunks_without_a_source_are_never_capped_out():
    rows = [{"similarity": 0.9, "metadata": {}} for _ in range(4)]
    assert len(cap_per_document(rows, limit=4, max_per_doc=1)) == 4


def test_an_empty_result_set_is_handled():
    assert cap_per_document([], limit=5) == []
