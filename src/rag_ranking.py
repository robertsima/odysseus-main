"""Ranking signals layered on top of the hybrid vector+keyword RAG score.

Three problems that plain similarity search cannot express, and what this
module does about each:

**Temporal validity.** A knowledge base accumulates. The note that recorded a
decision in 2024 is every bit as *similar* to "what's our current deploy
process" as the one that superseded it in 2026, so retrieval hands the model
both and it answers from whichever it read first. :func:`recency_factor` and
:func:`temporal_multiplier` bias ranking toward recent notes — gently by
default, hard when the query itself asks about the present.

**Vocabulary that is not prose.** Vault organisation lives in tags and aliases.
Someone searching "#project notes" or by a note's alias is naming a set
precisely, and the plain keyword score — query-token overlap against chunk
prose — barely registers it. :func:`tag_alias_score` gives that its own credit.

**Redundancy.** Five chunks of one long note is five copies of one source. It
crowds out the second opinion that would have revealed a conflict, and it is
the single largest avoidable cost in a retrieval context block.
:func:`cap_per_document` enforces breadth before depth.

Every function degrades to neutral on chunks that lack the metadata — an index
built before this existed keeps ranking exactly as it did.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src.vault_markdown import decode_list, list_contains

logger = logging.getLogger(__name__)

# Neutral point for every factor: a chunk we know nothing about must neither
# gain nor lose ground against one we do.
NEUTRAL = 0.5

DEFAULT_HALFLIFE_DAYS = 180.0
# Baseline temporal pressure. Small enough to act as a tie-break between
# otherwise comparable chunks and no more — most queries are not about time,
# and an old note is not a wrong note.
DEFAULT_TEMPORAL_WEIGHT = 0.05
# Applied instead when the query is explicitly about now. Large enough to
# reorder genuinely, bounded so a recent-but-irrelevant note still loses to a
# relevant older one.
DEFAULT_TEMPORAL_INTENT_WEIGHT = 0.30
DEFAULT_MAX_CHUNKS_PER_DOC = 2

# How much to trust each date provenance. A filesystem mtime moves on re-sync,
# restore-from-backup and whitespace fixes, none of which mean the content
# became newly true, so it pulls the factor only halfway toward what the raw
# decay says.
_DATE_CONFIDENCE = {
    "frontmatter": 1.0,
    "filename": 0.9,
    "mtime": 0.5,
    "": 0.0,
}

_SECONDS_PER_DAY = 86400.0

# Words that mean "as things stand", not "at some point". Deliberately narrow:
# a false positive here silently down-ranks every older note for a query that
# never asked about time.
_TEMPORAL_INTENT_WORDS = frozenset({
    "current", "currently", "latest", "recent", "recently", "now", "today",
    "yesterday", "tomorrow", "upcoming", "newest", "present", "still",
    "up-to-date", "nowadays", "these", "lately", "ongoing", "active",
})
_TEMPORAL_INTENT_PHRASES = (
    "right now", "as of", "up to date", "these days", "at the moment",
    "this week", "this month", "this year", "last week", "last month",
    "most recent", "latest version", "what changed", "has changed",
    "still true", "still valid", "no longer",
)

_TAG_TOKEN_RE = re.compile(r"#([A-Za-z0-9_\-/]+)")

# Credit awarded when a query token matches a chunk's tags/aliases. An explicit
# "#tag" is an unambiguous request for a set and gets full keyword credit; a
# bare word that happens to equal a tag is strong but not certain.
_EXPLICIT_TAG_CREDIT = 1.0
_BARE_TAG_CREDIT = 0.7


def _env_float(name: str, default: float, low: float, high: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring non-numeric %s=%r; using %s", name, raw, default)
        return default
    if not (low <= value <= high):
        logger.warning("Ignoring out-of-range %s=%r; using %s", name, raw, default)
        return default
    return value


def _env_int(name: str, default: int, low: int, high: int) -> int:
    return int(_env_float(name, float(default), float(low), float(high)))


def halflife_days() -> float:
    return _env_float("ODYSSEUS_RAG_RECENCY_HALFLIFE_DAYS", DEFAULT_HALFLIFE_DAYS, 1.0, 36500.0)


def temporal_weight(intent: bool = False) -> float:
    if intent:
        return _env_float(
            "ODYSSEUS_RAG_TEMPORAL_INTENT_WEIGHT", DEFAULT_TEMPORAL_INTENT_WEIGHT, 0.0, 0.9
        )
    return _env_float("ODYSSEUS_RAG_TEMPORAL_WEIGHT", DEFAULT_TEMPORAL_WEIGHT, 0.0, 0.9)


def max_chunks_per_document() -> int:
    """0 disables the cap."""
    return _env_int("ODYSSEUS_RAG_MAX_CHUNKS_PER_DOC", DEFAULT_MAX_CHUNKS_PER_DOC, 0, 50)


def link_expansion_enabled() -> bool:
    raw = os.environ.get("ODYSSEUS_RAG_LINK_EXPANSION")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


# ---------------------------------------------------------------------------
# Temporal
# ---------------------------------------------------------------------------


def query_has_temporal_intent(query: str) -> bool:
    """Whether the query is asking about the present rather than the record."""
    if not query or not isinstance(query, str):
        return False
    lowered = query.lower()
    if any(phrase in lowered for phrase in _TEMPORAL_INTENT_PHRASES):
        return True
    words = set(re.findall(r"[a-z0-9\-]+", lowered))
    return bool(words & _TEMPORAL_INTENT_WORDS)


def recency_factor(
    doc_date: Optional[float],
    date_source: str = "",
    now: Optional[float] = None,
) -> float:
    """A 0..1 freshness score, ``NEUTRAL`` when the date is unknown.

    Exponential decay by half-life, then pulled back toward neutral in
    proportion to how much the date's provenance is worth. A note dated in the
    future (scheduled entries, journals written ahead) is treated as current
    rather than clamped to nonsense.
    """
    if not doc_date:
        return NEUTRAL
    try:
        stamp = float(doc_date)
    except (TypeError, ValueError):
        return NEUTRAL

    reference = float(now if now is not None else time.time())
    age_days = max(0.0, (reference - stamp) / _SECONDS_PER_DAY)
    raw = 0.5 ** (age_days / halflife_days())

    confidence = _DATE_CONFIDENCE.get((date_source or "").strip().lower(), 0.7)
    return NEUTRAL + (raw - NEUTRAL) * confidence


def temporal_multiplier(recency: float, intent: bool) -> float:
    """Score multiplier for a given freshness. Always in ``[1-w, 1]``."""
    weight = temporal_weight(intent)
    if weight <= 0:
        return 1.0
    return (1.0 - weight) + weight * max(0.0, min(1.0, recency))


def temporal_factor(meta: Any, intent: bool, now: Optional[float] = None) -> float:
    """The multiplier a chunk's date earns, as a standalone value.

    Returned separately from the score so a caller can apply the same factor to
    more than one term. ``rag_vector`` needs that: naming a document floors its
    score above everything unnamed, and recency has to break ties *inside* that
    floored group without letting a chunk fall out of it. Multiplying the
    blended score alone would be silently discarded by the floor.
    """
    if not isinstance(meta, dict):
        return 1.0
    factor = recency_factor(meta.get("doc_date"), str(meta.get("doc_date_source") or ""), now=now)
    return temporal_multiplier(factor, intent)


# ---------------------------------------------------------------------------
# Tags / aliases
# ---------------------------------------------------------------------------


def query_tag_tokens(query: str) -> set:
    """Tags the query names explicitly, as bare names without the ``#``."""
    if not query or not isinstance(query, str):
        return set()
    return {t.strip("/").lower() for t in _TAG_TOKEN_RE.findall(query) if t.strip("/")}


def tag_alias_score(query: str, query_words: Iterable[str], meta: Any) -> float:
    """Credit in ``[0, 1]`` for a query naming this chunk's tags or aliases.

    Nested Obsidian tags match on any segment, so ``#project`` finds a note
    tagged ``#project/odysseus`` — that nesting exists precisely so the parent
    can be used as a filter.
    """
    if not isinstance(meta, dict):
        return 0.0
    tags = decode_list(meta.get("tags"))
    aliases = decode_list(meta.get("aliases"))
    if not tags and not aliases:
        return 0.0

    segments = set()
    for tag in tags:
        segments.add(tag)
        segments.update(part for part in tag.split("/") if part)

    explicit = query_tag_tokens(query)
    if explicit & segments:
        return _EXPLICIT_TAG_CREDIT

    words = {str(w).strip().lower() for w in query_words if str(w).strip()}
    if not words:
        return 0.0
    # Aliases are frequently multi-word ("ai mind"); match those against the
    # raw query text rather than the token set.
    lowered = (query or "").lower()
    for alias in aliases:
        if not alias:
            continue
        if alias in words or (" " in alias and alias in lowered):
            return _BARE_TAG_CREDIT
    if words & segments:
        return _BARE_TAG_CREDIT
    return 0.0


# ---------------------------------------------------------------------------
# Link graph
# ---------------------------------------------------------------------------


def collect_link_targets(
    results: Sequence[Dict[str, Any]],
    depth_limit: int,
    exclude: Optional[set] = None,
) -> List[str]:
    """Note keys linked to from the top *depth_limit* results.

    The vault's ``[[wikilinks]]`` are an explicit, human-curated statement that
    two notes belong together — the same cross-branch relationship a topic tree
    tries to infer, except already written down. Following them is how a query
    that lands on one note reaches the constraint recorded in another.
    """
    seen = set(exclude or ())
    targets: List[str] = []
    for row in list(results)[:max(0, depth_limit)]:
        meta = row.get("metadata")
        if not isinstance(meta, dict):
            continue
        for key in decode_list(meta.get("links")):
            if key and key not in seen:
                seen.add(key)
                targets.append(key)
    return targets


def result_note_keys(results: Iterable[Dict[str, Any]]) -> set:
    keys = set()
    for row in results:
        meta = row.get("metadata")
        if isinstance(meta, dict):
            key = meta.get("note_key")
            if key:
                keys.add(str(key).lower())
    return keys


def chunk_links_to(meta: Any, targets: Iterable[str]) -> bool:
    """Whether a chunk's own note is one of *targets*."""
    if not isinstance(meta, dict):
        return False
    key = str(meta.get("note_key") or "").lower()
    return bool(key) and key in set(targets)


def linked_by(meta: Any, key: str) -> bool:
    return list_contains(meta.get("links") if isinstance(meta, dict) else None, key)


# ---------------------------------------------------------------------------
# Diversity
# ---------------------------------------------------------------------------


def cap_per_document(
    results: Sequence[Dict[str, Any]],
    limit: int,
    max_per_doc: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Take the top *limit* results, at most *max_per_doc* from any one file.

    Overflow is not discarded: if breadth alone cannot fill *limit* — a vault
    where the answer genuinely does live in one long note — the held-back
    chunks are appended in score order. So the cap costs nothing when there is
    nothing else to say, and buys source diversity whenever there is.
    """
    if max_per_doc is None:
        max_per_doc = max_chunks_per_document()
    ordered = list(results)
    if max_per_doc <= 0 or limit <= 0:
        return ordered[:limit] if limit > 0 else []

    counts: Dict[str, int] = {}
    kept: List[Dict[str, Any]] = []
    overflow: List[Dict[str, Any]] = []
    for row in ordered:
        meta = row.get("metadata")
        source = ""
        if isinstance(meta, dict):
            source = str(meta.get("source") or meta.get("filename") or "")
        if not source:
            kept.append(row)
            continue
        if counts.get(source, 0) >= max_per_doc:
            overflow.append(row)
            continue
        counts[source] = counts.get(source, 0) + 1
        kept.append(row)
        if len(kept) >= limit:
            return kept[:limit]

    if len(kept) < limit:
        kept.extend(overflow[: limit - len(kept)])
    return kept[:limit]
