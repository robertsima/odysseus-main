"""Query enhancement, entity extraction, and cache duration helpers."""

import re
import logging
from datetime import timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Query processing helpers
# ----------------------------------------------------------------------
def _detect_question_type(query: str) -> Optional[str]:
    """Return the leading question word if present (who, what, when, where, why, how)."""
    if not isinstance(query, str):
        return None
    q = query.strip().lower()
    for word in ("who", "what", "when", "where", "why", "how"):
        # Require a whole-word match: a bare prefix mis-flags ordinary queries
        # like "whatsapp pricing" (-> what) or "however ..." (-> how), which
        # then get spurious boost terms OR-appended in enhance_query.
        if q == word or q.startswith(word + " "):
            return word
    return None


def _extract_entities(query: str) -> Dict[str, List[str]]:
    """Lightweight entity extraction: capitalized words and date patterns."""
    if not isinstance(query, str):
        return {"names": [], "dates": []}
    entities: Dict[str, List[str]] = {"names": [], "dates": []}
    qtype = _detect_question_type(query)
    cleaned = query
    if qtype:
        cleaned = re.sub(rf"^{qtype}\b", "", cleaned, flags=re.I).strip()
    # Unicode-aware capitalized-word (name) detection. The old [A-Z][a-zA-Z]+
    # class missed non-ASCII names like "İstanbul"/"Zürich" (dropped) and
    # "São" (shredded). Keep the ASCII behaviour — the word boundary already
    # excludes camelCase mid-word capitals — by requiring an all-alphabetic
    # token of length > 1 whose first character is uppercase.
    for token in re.findall(r"\b\w+\b", cleaned):
        if len(token) > 1 and token[0].isupper() and token.isalpha():
            entities["names"].append(token)
    for year in re.findall(r"\b(?:19|20)\d{2}\b", cleaned):
        entities["dates"].append(year)
    month_day_year = re.findall(
        r"\b(?:Jan|January|Feb|February|Mar|March|Apr|April|May|Jun|June|Jul|July|Aug|August|Sep|Sept|September|Oct|October|Nov|November|Dec|December)\s+\d{1,2},?\s*\d{4}\b",
        cleaned,
        flags=re.I,
    )
    entities["dates"].extend(month_day_year)
    return entities


def _split_multi_part(query: str) -> List[str]:
    """Split a query into sub-queries on common conjunctions."""
    if not isinstance(query, str):
        return []
    parts = re.split(r"\s+and\s+|\s+or\s+|;", query, flags=re.I)
    return [p.strip() for p in parts if p.strip()]


def _extract_site_filter(query: str) -> Tuple[str, Optional[str]]:
    """Detect a 'site:example.com' token. Returns (query_without_token, site_or_None)."""
    if not isinstance(query, str):
        return "", None
    match = re.search(r"\bsite:([^\s]+)", query, flags=re.I)
    if match:
        site = match.group(1)
        new_query = re.sub(r"\bsite:[^\s]+", "", query, flags=re.I).strip()
        return new_query, site
    return query, None


# ----------------------------------------------------------------------
# Simplified retry query
# ----------------------------------------------------------------------
# Research agents write 9-16 word keyword soups ("2025 agent infrastructure
# reliability memory state tool call idempotency SDK products gaps developer
# tools"). The web engines behind SearXNG answer those with pages matching one
# word -- in the 2026-09-27 logs the relevance gate discarded all five rows for
# 35 of 70 such queries, and the rest leaned on dictionary pages for "offline"
# or "official". A shorter query made of the leading subject terms is what a
# person would retry with, so the provider chain gets one more pass with it.
MAX_SIMPLIFIED_TERMS = 6

_OPERATOR_RE = re.compile(
    r"\b(?:intitle|inurl|intext|allintitle|allinurl|allintext|filetype|ext|after|before|"
    r"lang|related|cache|define):\S+",
    flags=re.I,
)
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_SIMPLIFY_DROP = {
    # function words
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for", "from",
    "how", "in", "is", "it", "of", "on", "or", "the", "to", "vs", "what", "when",
    "where", "which", "who", "why", "with", "not",
    # research scaffolding that describes the kind of page wanted, not the subject
    "official", "documentation", "docs", "doc", "discussion", "discussions",
    "evidence", "example", "examples", "case", "study", "studies", "paper",
    "papers", "report", "reports", "survey", "issue", "issues", "github",
    "competitors", "alternatives", "products", "pain", "points", "latest",
    "current", "overview", "guide", "review", "reviews", "analysis",
}


def simplify_query(query: str, max_terms: int = MAX_SIMPLIFIED_TERMS) -> str:
    """Return a shorter keyword query for a retry, keeping any ``site:`` scope.

    Drops quotes, boolean operators, exclusions, search operators other than
    ``site:``, bare years (the time filter already carries recency), and
    scaffolding words; then keeps the first ``max_terms`` distinct terms.
    Returns the input unchanged when nothing useful would remain.
    """
    if not isinstance(query, str):
        return ""
    rest, site = _extract_site_filter(query)
    rest = _OPERATOR_RE.sub(" ", rest)
    rest = re.sub(r"[\"“”„()\[\]{}]", " ", rest)
    terms: List[str] = []
    seen = set()
    for raw in rest.split():
        if raw in ("OR", "AND", "NOT", "|", "&") or raw.startswith("-"):
            continue
        token = raw.strip(".,;:!?'`*")
        low = token.casefold()
        if (not re.search(r"\w", token) or low in _SIMPLIFY_DROP
                or _YEAR_RE.match(token) or low in seen):
            continue
        seen.add(low)
        terms.append(token)
        if len(terms) >= max_terms:
            break
    if not terms:
        return query
    simplified = " ".join(terms)
    return f"site:{site} {simplified}" if site else simplified


def _boost_entities_in_query(base_query: str, entities: Dict[str, List[str]]) -> str:
    """Append extracted entities to the query using OR to increase relevance."""
    parts = [base_query]
    if entities.get("names"):
        parts.append(" OR ".join(f'"{n}"' for n in entities["names"]))
    if entities.get("dates"):
        parts.append(" OR ".join(f'"{d}"' for d in entities["dates"]))
    return " ".join(parts)


def enhance_query(original_query: str) -> Tuple[str, Optional[str]]:
    """Process the original query: site filter, question type boosts, entity extraction."""
    if not isinstance(original_query, str):
        original_query = ""
    query_without_site, site = _extract_site_filter(original_query)
    sub_queries = _split_multi_part(query_without_site)

    enhanced_subs: List[str] = []
    for sub in sub_queries:
        qtype = _detect_question_type(sub)
        boost_keywords = []
        if qtype == "who":
            boost_keywords.append("person")
        elif qtype == "when":
            boost_keywords.append("date")
        elif qtype == "where":
            boost_keywords.append("location")
        elif qtype == "why":
            boost_keywords.append("reason")
        elif qtype == "how":
            boost_keywords.append("method")
        entities = _extract_entities(sub)
        boosted = _boost_entities_in_query(sub, entities)
        if boost_keywords:
            boosted = f'({boosted}) OR ({" OR ".join(boost_keywords)})'
        enhanced_subs.append(boosted)

    final_query = " AND ".join(f"({s})" for s in enhanced_subs)
    if site:
        final_query = f"{final_query} site:{site}"
    return final_query, site


def build_enhanced_query(query: str, time_filter: str = None) -> str:
    """Build an enhanced search query with optional time filtering."""
    enhanced_query, _ = enhance_query(query)

    if time_filter:
        time_map = {"day": "d", "week": "w", "month": "m", "year": "y"}
        if time_filter in time_map:
            enhanced_query = f"{enhanced_query} after:{time_map[time_filter]}"
            logger.info(f"Added time filter '{time_filter}' to query")

    logger.info(f"Enhanced query: '{query}' -> '{enhanced_query}'")
    return enhanced_query


# ----------------------------------------------------------------------
# Cache duration helpers
# ----------------------------------------------------------------------
def _is_news_query(query: str) -> bool:
    """Lightweight heuristic to decide if a query is news-oriented."""
    news_terms = {"news", "latest", "breaking", "today", "today's", "current", "updates", "happening"}
    if not isinstance(query, str):
        return False
    tokens = set(re.findall(r"\b\w+\b", query.lower()))
    return bool(tokens & news_terms)


def _cache_duration_for_query(query: str) -> timedelta:
    """News queries -> 30 minutes, reference queries -> 24 hours."""
    if _is_news_query(query):
        return timedelta(minutes=30)
    return timedelta(hours=24)
