"""rag_ab_compare.py

Measure what vault-aware retrieval actually changed on a live index.

There are two independent halves to the change, and they need different
evidence:

**Indexing** — whether your notes carry frontmatter dates, tags, aliases,
wikilinks and headings at all. If they don't, several ranking signals have
nothing to act on and retrieval is *correctly* unchanged. ``--coverage``
answers this by reading the metadata off every stored chunk. It is also how
you confirm the one-time re-index actually ran: a chunk written by the old
indexer has no ``note_key``.

**Ranking** — whether the new signals reorder anything for your queries. Every
signal is env-gated and applied at query time, so both configurations can be
run back to back against the same index with no re-index in between. That is
what the query mode does: it runs each query with the new ranking off, then on,
and diffs the two result lists.

What this cannot A/B is the chunk *format* (heading-aware splitting and the
provenance header). Those are baked in at index time, so the only comparison
would be a full re-index in both formats. ``--coverage`` is the proxy.

Run it where ChromaDB is reachable — inside the app container, not on the host.
Compose is not required; find the container with ``docker ps --format
'{{.Names}}'``:

    docker exec <container> python scripts/rag_ab_compare.py --coverage
    docker exec <container> python scripts/rag_ab_compare.py \\
        "what is my current approach to X" "#project notes" "..."
"""
from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
from collections import Counter
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.vault_markdown import decode_list, format_date  # noqa: E402

# Values that reproduce pre-change ranking: every signal added on top of the
# base hybrid score, switched off. The base score (0.7 vector + 0.3 keyword)
# and the filename-match floor are untouched by all of these — they predate
# this work and are not what is being measured.
BASELINE_ENV = {
    "ODYSSEUS_RAG_TEMPORAL_WEIGHT": "0",
    "ODYSSEUS_RAG_TEMPORAL_INTENT_WEIGHT": "0",
    "ODYSSEUS_RAG_TAG_CREDIT": "0",
    "ODYSSEUS_RAG_LINK_EXPANSION": "0",
    "ODYSSEUS_RAG_MAX_CHUNKS_PER_DOC": "0",
    "ODYSSEUS_RAG_FOCUSED_CAP_MULTIPLIER": "1",
}


@contextlib.contextmanager
def env(**overrides: str):
    """Temporarily set environment variables, restoring the previous values."""
    previous = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def _all_metadata(rag) -> List[Dict[str, Any]]:
    metas: List[Dict[str, Any]] = []
    for lane_name, collection in rag._active_collections():
        try:
            if collection.count() == 0:
                continue
            got = collection.get(include=["metadatas"])
        except Exception as e:
            print(f"  ! could not read {lane_name} lane: {e}")
            continue
        for meta in got.get("metadatas") or []:
            if isinstance(meta, dict):
                metas.append(meta)
    return metas


def _pct(count: int, total: int) -> str:
    return f"{count:>7,}  ({(100.0 * count / total if total else 0):5.1f}%)"


def report_coverage(rag) -> None:
    metas = _all_metadata(rag)
    total = len(metas)
    print(f"\nIndexed chunks: {total:,}")
    if not total:
        print("Nothing indexed. Check the personal-documents directories are tracked.")
        return

    markdown = [m for m in metas if str(m.get("type", "")).lower() in (".md", ".markdown")]
    vault_format = [m for m in metas if m.get("note_key")]

    print("\n-- Indexing --------------------------------------------------")
    print(f"  Markdown chunks          {_pct(len(markdown), total)}")
    print(f"  Written by vault indexer {_pct(len(vault_format), total)}")
    if markdown and len(vault_format) < len(markdown):
        stale = len(markdown) - len(vault_format)
        print(
            f"  ! {stale:,} Markdown chunk(s) predate vault-aware indexing.\n"
            f"    The one-time re-index has not covered them. Check the app log for\n"
            f"    'Vault scan state is format vN, expected vM', or that the directory\n"
            f"    is in the tracked list (Settings -> Personal documents)."
        )

    if not markdown:
        print("\n  No Markdown in the index — none of the vault signals can apply.")
        return

    scope = vault_format or markdown
    scoped = len(scope)

    print("\n-- Signals available on Markdown chunks ----------------------")
    dated = [m for m in scope if m.get("doc_date")]
    print(f"  Have a date              {_pct(len(dated), scoped)}")
    sources = Counter(str(m.get("doc_date_source") or "none") for m in scope)
    for name, count in sources.most_common():
        note = {
            "frontmatter": "strongest — full weight",
            "filename": "strong",
            "mtime": "weak — discounted, moves on re-sync",
            "none": "no temporal signal",
        }.get(name, "")
        print(f"      via {name:<12} {_pct(count, scoped)}   {note}")

    print(f"  Have tags                {_pct(sum(1 for m in scope if m.get('tags')), scoped)}")
    print(f"  Have aliases             {_pct(sum(1 for m in scope if m.get('aliases')), scoped)}")
    print(f"  Have wikilinks           {_pct(sum(1 for m in scope if m.get('links')), scoped)}")
    print(f"  Have a heading path      {_pct(sum(1 for m in scope if m.get('heading_path')), scoped)}")

    tags = Counter(t for m in scope for t in decode_list(m.get("tags")))
    if tags:
        top = ", ".join(f"#{t} ({n})" for t, n in tags.most_common(12))
        print(f"\n  Most common tags: {top}")

    if dated:
        stamps = sorted(float(m["doc_date"]) for m in dated)
        print(f"  Date range: {format_date(stamps[0])} .. {format_date(stamps[-1])}")

    print("\n-- Reading this ----------------------------------------------")
    advised = False
    if len(dated) >= scoped * 0.5 and tags and any(m.get("links") for m in scope):
        # Silence used to mean "every check passed", which reads as a broken
        # report rather than a clean bill of health.
        print("  Your vault carries every signal the ranking can use. If retrieval")
        print("  still looks unchanged, the queries are ones where relevance alone")
        print("  was already right — which is the intended behaviour, not a failure.")
        print("  Use the query mode to find the ones where it is not.")
        advised = True
    if len(dated) < scoped * 0.5:
        print("  Most notes have no date, so recency ranking is mostly inert.")
        print("  Adding `updated:` to the frontmatter of notes you revise is the")
        print("  single highest-value change you can make to your vault.")
        advised = True
    if sources.get("mtime", 0) > scoped * 0.5:
        print("  Dates come mainly from file mtime, which is deliberately discounted")
        print("  (a re-sync moves it without the content changing). Frontmatter dates")
        print("  would carry twice the weight.")
        advised = True
    if not tags:
        print("  No tags anywhere — tag/alias scoring can never fire.")
        advised = True
    if not any(m.get("links") for m in scope):
        print("  No [[wikilinks]] — multi-source link expansion can never fire.")
        advised = True
    if not advised:
        print("  Nothing stands out; see the per-signal percentages above.")


# ---------------------------------------------------------------------------
# A/B
# ---------------------------------------------------------------------------


def _key(row: Dict[str, Any]) -> str:
    meta = row.get("metadata") or {}
    name = meta.get("filename") or meta.get("source") or row.get("id")
    heading = meta.get("heading_path")
    return f"{name}#{heading}" if heading else str(name)


def _describe(row: Dict[str, Any]) -> str:
    meta = row.get("metadata") or {}
    bits = [f"{row.get('similarity', 0):.3f}", _key(row)]
    stamp = format_date(meta.get("doc_date"))
    if stamp:
        bits.append(stamp)
    if row.get("retrieval_path") == "link":
        bits.append("via-link")
    return "  ".join(bits)


def compare(rag, query: str, k: int, owner: Optional[str], allow_private: bool) -> bool:
    with env(**BASELINE_ENV):
        before = rag.search(query, k=k, owner=owner, allow_private=allow_private)
    after = rag.search(query, k=k, owner=owner, allow_private=allow_private)

    before_keys = [_key(r) for r in before]
    after_keys = [_key(r) for r in after]
    changed = before_keys != after_keys

    print(f"\n{'=' * 70}\nQUERY  {query}")
    from src.rag_ranking import query_has_temporal_intent, query_tag_tokens

    flags = []
    if query_has_temporal_intent(query):
        flags.append("temporal-intent")
    if query_tag_tokens(query):
        flags.append("names-tags")
    print(f"       [{', '.join(flags) if flags else 'no special intent detected'}]")

    print("\n  before (new ranking off)          after (new ranking on)")
    for i in range(max(len(before), len(after))):
        left = _describe(before[i]) if i < len(before) else ""
        right = _describe(after[i]) if i < len(after) else ""
        mark = " " if left[:60] == right[:60] else ">"
        print(f"  {mark} {left:<32.32}  {right:<32.32}")

    gained = [key for key in after_keys if key not in before_keys]
    lost = [key for key in before_keys if key not in after_keys]
    if gained:
        print(f"  + gained: {', '.join(gained)}")
    if lost:
        print(f"  - lost:   {', '.join(lost)}")
    if not changed:
        print("  = identical. The new signals had nothing to act on for this query.")
    elif not gained and not lost:
        print("  = same documents, reordered.")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure vault-aware retrieval against the live index.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("queries", nargs="*", help="Queries to A/B compare")
    parser.add_argument("--coverage", action="store_true", help="Report indexed metadata coverage")
    parser.add_argument("--queries-file", help="File of queries, one per line")
    parser.add_argument("-k", type=int, default=5, help="Results per query (default 5)")
    parser.add_argument("--owner", default=None, help="Scope to one owner, as the app does")
    parser.add_argument(
        "--public-only",
        action="store_true",
        help="Exclude private notes, as a hosted-API turn does",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    queries = list(args.queries)
    if args.queries_file:
        with open(args.queries_file, "r", encoding="utf-8") as handle:
            queries.extend(line.strip() for line in handle if line.strip())

    if not queries and not args.coverage:
        parser.error("give at least one query, or --coverage")

    from src.rag_vector import VectorRAG

    rag = VectorRAG()
    if not rag.healthy:
        print("RAG is not healthy — ChromaDB unreachable or no embedding lane.")
        print("Run this inside the app container: docker compose exec odysseus ...")
        return 1

    if args.coverage:
        report_coverage(rag)

    if queries:
        changed = sum(
            compare(rag, q, args.k, args.owner, not args.public_only) for q in queries
        )
        print(f"\n{'=' * 70}")
        print(f"{changed}/{len(queries)} queries returned a different result list.")
        if not changed:
            print(
                "All identical. Either your notes carry none of the signals (run\n"
                "--coverage), or these queries are ones where relevance alone was\n"
                "already right — which is the intended behaviour, not a failure."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
