"""End-to-end ranking behaviour for a Markdown knowledge base.

test_rag_ranking.py pins the individual signals. These pin what they do to the
order results actually come back in — and, just as importantly, what they leave
alone, because every one of these changes rides on top of a hybrid score that
was already tuned.
"""
import os
import time
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import src.rag_vector as rag_vector
from src.rag_vector import VectorRAG
from src.vault_markdown import encode_list


def _epoch(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc).timestamp()


NOW = time.time()
RECENT = NOW - 5 * 86400
ANCIENT = NOW - 1500 * 86400


class _Lane:
    name = "test"

    def count(self):
        return 100


def _rows_payload(rows):
    return {
        "ids": [[r["id"] for r in rows]],
        "distances": [[r["distance"] for r in rows]],
        "documents": [[r["document"] for r in rows]],
        "metadatas": [[r["metadata"] for r in rows]],
    }


def _install(monkeypatch, direct_rows, linked_rows=None):
    """Fake the lane layer, routing the link-expansion pass to its own rows.

    Link expansion is recognised by the ``note_key``/``$in`` clause it adds to
    the filter, which is the only thing that distinguishes it from the primary
    query at this layer.
    """
    calls = []

    def fake_query_lanes(lanes, query, n_results, include, where=None, where_document=None, **kw):
        calls.append({"where": where, "where_document": where_document})
        if _is_link_pass(where):
            return [(_Lane(), _rows_payload(linked_rows))] if linked_rows else []
        if where_document is not None:
            return []
        return [(_Lane(), _rows_payload(direct_rows))]

    def _is_link_pass(where):
        if not isinstance(where, dict):
            return False
        if "note_key" in where:
            return True
        return any(
            isinstance(c, dict) and "note_key" in c for c in where.get("$and", []) or []
        )

    monkeypatch.setattr(rag_vector, "query_lanes", fake_query_lanes)
    monkeypatch.setattr(rag_vector, "lane_count", lambda lanes: 1)

    rag = VectorRAG.__new__(VectorRAG)
    rag._lanes = [_Lane()]
    rag._healthy = True
    return rag, calls


def _note(note_id, *, distance, filename, doc_date=None, source_kind="frontmatter",
          tags=(), links=(), text="Some prose about the deploy process.", heading=""):
    return {
        "id": note_id,
        "distance": distance,
        "document": text,
        "metadata": {
            "filename": filename,
            "source": f"/vault/{filename}",
            "note_key": filename.rsplit(".", 1)[0].lower(),
            "doc_date": doc_date or 0.0,
            "doc_date_source": source_kind if doc_date else "",
            "tags": encode_list(list(tags)),
            "links": encode_list(list(links)),
            "heading_path": heading,
        },
    }


# -- temporal validity -----------------------------------------------------


def test_a_current_note_beats_a_superseded_one_for_a_present_tense_query(monkeypatch):
    # The two notes say contradictory things and read almost identically, so
    # similarity alone cannot separate them. The date is the only signal that
    # distinguishes "what we do" from "what we used to do".
    old = _note("old", distance=0.30, filename="Deploy 2022.md", doc_date=ANCIENT)
    new = _note("new", distance=0.34, filename="Deploy.md", doc_date=RECENT)
    rag, _ = _install(monkeypatch, [old, new])

    results = rag.search("what is our current deploy process", k=5)

    assert results[0]["metadata"]["filename"] == "Deploy.md"


def test_without_temporal_intent_the_better_match_still_wins(monkeypatch):
    # No regression: an old note is not a wrong note. A question that never
    # asked about time is ordered by relevance, as it always was.
    old = _note("old", distance=0.30, filename="Deploy 2022.md", doc_date=ANCIENT)
    new = _note("new", distance=0.34, filename="Deploy.md", doc_date=RECENT)
    rag, _ = _install(monkeypatch, [old, new])

    results = rag.search("how does the deploy pipeline handle volumes", k=5)

    assert results[0]["metadata"]["filename"] == "Deploy 2022.md"


def test_recency_cannot_outrank_a_document_the_query_names(monkeypatch):
    # Naming a file is an explicit request for that file. It must not lose to a
    # newer note just because the question also said "recently".
    named = _note("named", distance=0.60, filename="08-08-2026.md", doc_date=_epoch(2026, 8, 8),
                  text="Source: 08-08-2026.md 08-08-2026\nQuiet day.")
    fresh = _note("fresh", distance=0.20, filename="Today.md", doc_date=NOW)
    rag, _ = _install(monkeypatch, [fresh, named])

    results = rag.search("what did I recently write on 08-08-2026", k=5)

    assert results[0]["metadata"]["filename"] == "08-08-2026.md"


def test_an_undated_index_ranks_exactly_as_it_did_before(monkeypatch):
    # Chunks written before dates were extracted carry none of this metadata.
    a = {"id": "a", "distance": 0.30, "document": "alpha deploy", "metadata": {"filename": "a.md", "source": "/v/a.md"}}
    b = {"id": "b", "distance": 0.40, "document": "beta deploy", "metadata": {"filename": "b.md", "source": "/v/b.md"}}
    rag, _ = _install(monkeypatch, [b, a])

    results = rag.search("current deploy process", k=5)

    assert [r["id"] for r in results] == ["a", "b"]


# -- tags ------------------------------------------------------------------


def test_a_tag_query_surfaces_a_tagged_note_over_a_closer_prose_match(monkeypatch):
    # "#project" names a set deliberately. The tag appears once, in a
    # frontmatter block, so prose overlap barely registers it.
    tagged = _note("tagged", distance=0.45, filename="Odysseus.md", tags=["project", "ai"])
    prose = _note("prose", distance=0.32, filename="Musings.md",
                  text="I have been thinking about a project for a while now.")
    rag, _ = _install(monkeypatch, [prose, tagged])

    results = rag.search("#project notes", k=5)

    assert results[0]["metadata"]["filename"] == "Odysseus.md"


# -- multi-source retrieval ------------------------------------------------


def test_a_linked_note_is_pulled_in_even_when_it_matches_nothing(monkeypatch):
    # The classic cross-branch case: the question is about the party, the
    # constraint that makes the answer correct ("Sarah is allergic to peanuts")
    # lives in a note the query shares no vocabulary with. The vault already
    # records the connection as a wikilink.
    party = _note("party", distance=0.20, filename="Party Plan.md", links=["sarah"],
                  text="Planning the party. Guests: see [[Sarah]].")
    sarah = _note("sarah", distance=0.85, filename="Sarah.md",
                  text="Sarah is allergic to peanuts.")
    rag, calls = _install(monkeypatch, [party], linked_rows=[sarah])

    results = rag.search("what should I bake for the party", k=5)

    assert [r["metadata"]["filename"] for r in results] == ["Party Plan.md", "Sarah.md"]
    assert results[1]["retrieval_path"] == "link"
    assert any("note_key" in str(c["where"]) for c in calls)


def test_link_expansion_keeps_the_owner_and_privacy_scope(monkeypatch):
    # A second query that dropped the scope would reach another user's notes,
    # or private ones on a hosted-API turn.
    party = _note("party", distance=0.20, filename="Party Plan.md", links=["sarah"])
    rag, calls = _install(monkeypatch, [party], linked_rows=[])

    rag.search("what should I bake", k=5, owner="rob", allow_private=False)

    link_call = next(c for c in calls if "note_key" in str(c["where"]))
    flat = str(link_call["where"])
    assert "rob" in flat and "public" in flat


def test_a_linked_note_does_not_outrank_a_direct_hit(monkeypatch):
    # Relevance by association is real but weaker than relevance by match.
    direct = _note("direct", distance=0.30, filename="Direct.md", links=["other"])
    linked = _note("linked", distance=0.30, filename="Other.md")
    rag, _ = _install(monkeypatch, [direct], linked_rows=[linked])

    results = rag.search("anything about the deploy", k=5)

    assert results[0]["id"] == "direct"
    assert results[0]["similarity"] > results[1]["similarity"]


def test_expansion_is_skipped_when_disabled(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_RAG_LINK_EXPANSION", "0")
    party = _note("party", distance=0.20, filename="Party Plan.md", links=["sarah"])
    sarah = _note("sarah", distance=0.85, filename="Sarah.md")
    rag, calls = _install(monkeypatch, [party], linked_rows=[sarah])

    results = rag.search("what should I bake", k=5)

    assert [r["id"] for r in results] == ["party"]
    assert not any("note_key" in str(c["where"]) for c in calls)


def test_a_failing_expansion_pass_does_not_break_the_search(monkeypatch):
    # A backend without $in support must cost us the supporting note, not the
    # answer.
    party = _note("party", distance=0.20, filename="Party Plan.md", links=["sarah"])
    rag, _ = _install(monkeypatch, [party])

    def exploding(lanes, query, n_results, include, where=None, where_document=None, **kw):
        if where is not None and "note_key" in str(where):
            raise RuntimeError("$in unsupported")
        if where_document is not None:
            return []
        return [(_Lane(), _rows_payload([party]))]

    monkeypatch.setattr(rag_vector, "query_lanes", exploding)

    results = rag.search("what should I bake", k=5)

    assert [r["id"] for r in results] == ["party"]


# -- diversity -------------------------------------------------------------


def test_one_long_note_cannot_fill_the_whole_result_set(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_RAG_MAX_CHUNKS_PER_DOC", "2")
    rows = [
        _note(f"long{i}", distance=0.20 + i * 0.01, filename="Long.md", heading=f"S{i}")
        for i in range(4)
    ]
    rows += [
        _note("other1", distance=0.40, filename="Other.md"),
        _note("other2", distance=0.41, filename="Third.md"),
    ]
    rag, _ = _install(monkeypatch, rows)

    results = rag.search("deploy process details", k=4)

    sources = [r["metadata"]["filename"] for r in results]
    assert sources.count("Long.md") == 2
    assert "Other.md" in sources and "Third.md" in sources


def test_naming_a_tag_relaxes_the_diversity_cap(monkeypatch):
    # Same shape as the test above, but the query points at the notes by tag.
    # Breadth is no longer a service to the user here: they have already said
    # which notes they mean, so their best passages should not be traded for
    # weaker ones from notes nobody asked about.
    monkeypatch.setenv("ODYSSEUS_RAG_MAX_CHUNKS_PER_DOC", "2")
    rows = [
        _note(f"long{i}", distance=0.20 + i * 0.01, filename="Long.md",
              heading=f"S{i}", tags=["homelab"])
        for i in range(4)
    ]
    rows += [
        _note("other1", distance=0.40, filename="Other.md"),
        _note("other2", distance=0.41, filename="Third.md"),
    ]
    rag, _ = _install(monkeypatch, rows)

    results = rag.search("#homelab deploy process details", k=4)

    assert [r["metadata"]["filename"] for r in results].count("Long.md") == 4


def test_naming_a_file_relaxes_the_diversity_cap(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_RAG_MAX_CHUNKS_PER_DOC", "2")
    rows = [
        _note(f"long{i}", distance=0.20 + i * 0.01, filename="Runbook.md", heading=f"S{i}")
        for i in range(4)
    ]
    rows += [
        _note("other1", distance=0.40, filename="Other.md"),
        _note("other2", distance=0.41, filename="Third.md"),
    ]
    rag, _ = _install(monkeypatch, rows)

    results = rag.search("what does runbook say about deploys", k=4)

    assert [r["metadata"]["filename"] for r in results].count("Runbook.md") == 4


# -- indexing --------------------------------------------------------------


class _RecordingRag(VectorRAG):
    """VectorRAG with the Chroma write replaced by a recorder."""

    def __init__(self):
        self.written = []

    def add_documents_batch(self, docs):
        self.written.extend(docs)
        return {"success": True, "added_count": len(docs), "failed_count": 0}


def _rag():
    rag = _RecordingRag.__new__(_RecordingRag)
    rag.__init__()
    return rag


NOTE = """---
tags: [project, ai]
aliases: [AI Mind]
updated: 2026-08-14
---

# Vault Mind

Intro.

## Retrieval

Uses ChromaDB. See [[Odysseus Reference]].
"""


def test_indexing_a_note_records_its_vault_metadata(tmp_path):
    path = tmp_path / "Vault Mind.md"
    path.write_text(NOTE, encoding="utf-8")
    rag = _rag()

    indexed, failed = rag.index_file(str(path))

    assert indexed and not failed
    meta = rag.written[0][1]
    assert meta["note_key"] == "vault mind"
    assert meta["title"] == "Vault Mind"
    assert "|project|" in meta["tags"] and "|ai|" in meta["tags"]
    assert "|ai mind|" in meta["aliases"]
    assert "|odysseus reference|" in meta["links"]
    assert meta["doc_date"] == _epoch(2026, 8, 14)
    assert meta["doc_date_source"] == "frontmatter"


def test_every_chunk_of_a_note_carries_its_metadata(tmp_path):
    # Retrieval scores chunks, not files. Metadata on the first chunk only
    # would make a note findable by tag exactly once.
    path = tmp_path / "Long.md"
    path.write_text(
        "---\ntags: [x]\n---\n" + "".join(f"## S{i}\n\n{'word ' * 300}\n\n" for i in range(4)),
        encoding="utf-8",
    )
    rag = _rag()
    rag.index_file(str(path))

    assert len(rag.written) > 1
    assert all("|x|" in meta["tags"] for _text, meta in rag.written)


def test_frontmatter_is_not_indexed_as_prose(tmp_path):
    path = tmp_path / "Vault Mind.md"
    path.write_text(NOTE, encoding="utf-8")
    rag = _rag()
    rag.index_file(str(path))

    body = rag.written[0][0]
    assert "aliases:" not in body
    # ...but the same facts are present in the searchable header, because the
    # keyword score only ever sees chunk text.
    assert "#project" in body
    assert "Updated: 2026-08-14" in body


def test_a_non_markdown_file_is_indexed_exactly_as_before(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("Plain text content.", encoding="utf-8")
    rag = _rag()
    rag.index_file(str(path))

    text, meta = rag.written[0]
    assert text == "Source: notes.txt notes\nPlain text content."
    assert "tags" not in meta
    assert "note_key" not in meta


def test_an_unparseable_note_is_still_indexed(tmp_path, monkeypatch):
    # Losing a note from the index entirely is far worse than indexing it
    # without its tags.
    monkeypatch.setattr(
        rag_vector, "parse_markdown", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    path = tmp_path / "Broken.md"
    path.write_text("# Still real content\n\nProse.", encoding="utf-8")
    rag = _rag()

    indexed, failed = rag.index_file(str(path))

    assert indexed and not failed
    assert "Still real content" in rag.written[0][0]
    assert rag.written[0][1]["note_key"] == "broken"
