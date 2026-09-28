"""Core search orchestrators: searxng_search_results, comprehensive_web_search, config, cache invalidation."""

import json
import logging
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Set, Tuple
from urllib.parse import urlparse

from .analytics import (
    NetworkError,
    ParseError,
    RateLimitError,
    error_logger,
    _record_query,
)
from .cache import (
    SEARCH_CACHE_DIR,
    search_cache_index,
    generate_cache_key,
    cleanup_cache,
)
from .query import (
    _cache_duration_for_query,
    _extract_site_filter,
    github_issue_scope,
    normalize_site_wildcard,
    simplify_query,
)
from .ranking import rank_search_results
from . import resilience
from .providers import (
    github_issue_search,
    github_issue_search_enabled,
    github_row_summary,
    searxng_search_api,
    brave_search,
    duckduckgo_search,
    google_pse_search,
    tavily_search,
    serper_search,
    _get_search_settings,
    _get_provider_key,
    _get_result_count,
)
from .content import (
    fetch_webpage_content,
    extract_key_points,
    get_tldr,
    extract_quotes,
    extract_statistics,
)

logger = logging.getLogger(__name__)

# Chain step for GitHub-issue-scoped queries (not a user-selectable provider).
GITHUB_ISSUES = "github_issues"

# Attempt outcome for a provider skipped or cut short by the search deadline.
OUT_OF_TIME = "out of time"

# Share of a comprehensive search's budget kept back for page fetches, so a
# slow provider chain still leaves time to read the pages it found.
_FETCH_RESERVE_SHARE = 0.3
_FETCH_RESERVE_MAX = 8.0
_PAGE_FETCH_TIMEOUT = 8

# Search engines occasionally return a perfectly valid-looking result set for
# the wrong intent (for example, travel pages for a repository/poller query).
# Keep this list deliberately small: a query made entirely from these terms is
# too underspecified to reject, while a query with a distinctive term gets a
# cheap, deterministic relevance check before we fetch any pages.
_RELEVANCE_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for",
    "from", "how", "i", "in", "is", "it", "latest", "me", "of", "on", "or",
    "please", "search", "show", "tell", "the", "to", "today", "what", "when",
    "where", "which", "who", "why", "with", "would", "www", "http", "https",
    "com", "org", "net", "site", "after", "before", "lang", "intitle", "inurl",
    "filetype", "related", "cache", "define", "allintext", "allintitle", "allinurl",
    "intext", "not", "main",
    # These are useful prompt scaffolding, but do not identify a subject. They
    # also keep generic/navigational calls from being rejected on sparse
    # provider metadata.
    "test", "query", "web", "page", "pages", "result", "results",
    # Research scaffolding (the words simplify_query drops): they describe the
    # kind of page wanted, not its subject. "github" and "issues" in
    # particular matched the URL of *every* github.com issue, so a junk issue
    # passed a "GitHub issue <subject>" query on scaffolding alone.
    "official", "documentation", "docs", "doc", "discussion", "discussions",
    "evidence", "example", "examples", "case", "study", "studies", "paper",
    "papers", "report", "reports", "survey", "issue", "issues", "github",
    "competitors", "alternatives", "products", "pain", "points", "current",
    "overview", "guide", "review", "reviews", "analysis",
}

_YEAR_TOKEN_RE = re.compile(r"^(?:19|20)\d{2}$")


def _significant_query_tokens(query: str) -> Set[str]:
    """Return normalized subject tokens suitable for a relevance gate.

    Search operators (especially ``site:``), ordinary stopwords, research
    scaffolding and bare years (the time filter carries recency; a date in a
    junk snippet is not subject evidence) are left out. Keep other numeric
    IDs when they have at least two digits, since standards/issues/versions
    often use them as the only useful signal.
    """
    rest, _ = _site_scope(query or "")
    normalized = unicodedata.normalize("NFKC", rest).casefold()
    tokens = re.findall(r"\w+", normalized, flags=re.UNICODE)
    return {
        token for token in tokens
        if token not in _RELEVANCE_STOPWORDS
        and not _YEAR_TOKEN_RE.match(token)
        and ((len(token) >= 3 and not token.isdigit())
             or (token.isdigit() and len(token) >= 2))
    }


def _distinctive_query_tokens(query: str) -> Set[str]:
    """Tokens spelled like identifiers: CamelCase, snake_case, letters+digits.

    ``LongMemEval``, ``LlamaIndex``, ``refresh_ref_docs``, ``BM25``, ``mem0``
    name one thing; a row that mentions one of them is about the subject even
    when it shares no second query word (a real search engine's rows for
    "LongMemEval benchmark temporal reasoning knowledge updates" were dropped
    for saying "Benchmarking" and "Long-Term Memory" instead).
    """
    rest, _ = _site_scope(query or "")
    out: Set[str] = set()
    for raw in re.findall(r"\w+", unicodedata.normalize("NFKC", rest), flags=re.UNICODE):
        if len(raw) < 3 or raw.isdigit() or _YEAR_TOKEN_RE.match(raw):
            continue
        if (re.search(r"[a-z][A-Z]", raw) or "_" in raw.strip("_")
                or (re.search(r"[A-Za-z]", raw) and re.search(r"\d", raw))):
            low = raw.casefold()
            if low not in _RELEVANCE_STOPWORDS:
                out.add(low)
    return out


def _stem(token: str) -> str:
    """Crude suffix folding so "embeddings"/"embedding"/"embedded" meet.

    Applied identically to query and row tokens, so it only has to be
    consistent, not linguistically right.
    """
    t = token
    if len(t) > 4 and t.endswith("ies"):
        t = t[:-3] + "y"
    elif len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        t = t[:-1]
    for suffix in ("ing", "ed"):
        if len(t) - len(suffix) >= 4 and t.endswith(suffix):
            t = t[: -len(suffix)]
            break
    if len(t) > 4 and t.endswith("e"):
        t = t[:-1]
    return t


def _token_forms(tokens) -> Set[str]:
    forms: Set[str] = set()
    for token in tokens:
        forms.add(token)
        forms.add(_stem(token))
        squashed = re.sub(r"[_]", "", token)
        if squashed != token:
            forms.add(squashed)
    return forms


def _result_text_for_relevance(result: dict) -> str:
    """Build the searchable text for one provider row.

    Include title, snippet, and URL host/path. The URL is useful for repository
    names and stable identifiers, but query strings/fragments are intentionally
    excluded because they are often tracking or unrelated navigation text.
    """
    title = result.get("title", "")
    snippet = result.get("snippet", "")
    url = result.get("url", "")
    try:
        parsed = urlparse(url)
        url_text = " ".join((parsed.hostname or "", parsed.path or ""))
    except (TypeError, ValueError):
        url_text = ""
    return " ".join(str(part) for part in (title, snippet, url_text) if part)


def _result_forms(result: dict) -> Set[str]:
    """Normalized word forms present in a row (plus joined identifiers).

    ``llama_index``, ``llama-index`` and ``llama.index`` also yield
    ``llamaindex`` so they match a query that wrote ``LlamaIndex``.
    """
    text = unicodedata.normalize("NFKC", _result_text_for_relevance(result)).casefold()
    tokens = re.findall(r"\w+", text, flags=re.UNICODE)
    joined = [
        re.sub(r"[-_.]", "", chunk)
        for chunk in re.findall(r"\w+(?:[-_.]\w+)+", text, flags=re.UNICODE)
    ]
    return _token_forms(tokens) | set(joined)


# A query with this many significant tokens needs two of them in a row before
# the row counts as relevant. Long research queries otherwise let one-word
# matches through: the 2026-09-27 logs show merriam-webster.com/dictionary/
# offline, /official, /customer and investopedia term pages fetched ~80 times
# because "offline" or "customer" was one of 12 query words.
_STRICT_RELEVANCE_MIN_TOKENS = 5

# Hosts where being on the domain says nothing about the subject.
_GENERIC_SITE_HOSTS = {
    "github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "reddit.com",
    "stackoverflow.com", "stackexchange.com", "medium.com", "dev.to", "youtube.com",
    "x.com", "twitter.com", "news.ycombinator.com", "huggingface.co", "arxiv.org",
    "wikipedia.org", "en.wikipedia.org",
}


def _row_host(result: dict) -> str:
    try:
        return (urlparse(result.get("url", "")).hostname or "").lower()
    except (TypeError, ValueError, AttributeError):
        return ""


def _row_in_scope(result: dict, domain: Optional[str], path_prefix: Optional[str]) -> bool:
    """True when a row sits where a ``site:`` scope pointed with some precision.

    Either under the scope's path (``site:github.com/org/repo/issues`` ->
    ``/org/repo``) or on a domain specific enough to identify the subject
    (``site:lucide.dev``; not ``site:github.com``).
    """
    if not domain or not _url_on_site(result.get("url", ""), domain):
        return False
    if path_prefix:
        try:
            path = (urlparse(result.get("url", "")).path or "").casefold()
        except (TypeError, ValueError):
            return False
        return path == path_prefix or path.startswith(path_prefix + "/")
    return domain not in _GENERIC_SITE_HOSTS


def _keep_relevant_results(query: str, results: List[dict]) -> List[dict]:
    """Drop result rows with no meaningful token overlap with *query*.

    If the query has no meaningful tokens (for example a short navigational
    request made solely from stopwords), leave the provider's rows untouched.
    Otherwise a row survives when at least one significant query token occurs
    as a whole word (after light suffix folding) in its title, snippet, or URL
    host/path -- two distinct tokens once the query has
    ``_STRICT_RELEVANCE_MIN_TOKENS`` or more. One hit is enough on a long
    query when the hit is an identifier-like token (see
    ``_distinctive_query_tokens``) or the row sits inside the query's
    ``site:`` scope (``_row_in_scope``).

    Every drop is logged with the row's host, engine and hit count so the
    diagnostics bundle shows whether engines returned junk or the gate was
    too strict.
    """
    query_tokens = _significant_query_tokens(query)
    if not query_tokens:
        return results
    required = 2 if len(query_tokens) >= _STRICT_RELEVANCE_MIN_TOKENS else 1
    distinctive = _distinctive_query_tokens(query) & query_tokens
    token_forms = {token: _token_forms([token]) for token in query_tokens}
    _, domain = _site_scope(query or "")
    path_prefix = _site_path_prefix(query) if domain else None

    kept: List[dict] = []
    dropped: List[str] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        forms = _result_forms(result)
        hits = {token for token, variants in token_forms.items() if variants & forms}
        relevant = len(hits) >= required or bool(
            hits and (hits & distinctive or _row_in_scope(result, domain, path_prefix))
        )
        if relevant:
            kept.append(result)
        else:
            engine = result.get("engine") or ""
            dropped.append(
                f"{_row_host(result) or '?'}{'[' + engine + ']' if engine else ''}:{len(hits)}"
            )
    if len(kept) < len(results):
        logger.info(
            "Relevance gate for %r kept %d/%d result(s) (needs %d of %d terms); dropped host[engine]:hits = %s",
            query, len(kept), len(results), required, len(query_tokens), ", ".join(dropped[:12]),
        )
    return kept

# ========= CONFIG =========
SEARCH_CONFIG: Dict[str, Any] = {
    "primary_provider": "searxng",
}


def _is_secret_key(name: str) -> bool:
    """True for config keys that hold a credential (e.g. ``brave_api_key``)."""
    return name.endswith(("_api_key", "_key", "_token", "_secret"))


def get_search_config() -> Dict[str, Any]:
    """Get current search configuration including active provider info.

    Never returns stored API keys: callers — including the unauthenticated
    ``GET /api/search/config`` route — only need key *presence* via
    ``has_api_key``, not the secret itself (#1661).
    """
    config = SEARCH_CONFIG.copy()
    settings = _get_search_settings()
    provider = settings.get("search_provider", "searxng")
    config["active_provider"] = provider
    config["has_api_key"] = bool(_get_provider_key(provider))
    config["result_count"] = _get_result_count()
    if provider == "searxng":
        from .providers import _get_search_instance
        config["search_url"] = _get_search_instance()
    # Strip any string-valued credential so secrets never reach the response;
    # the boolean has_api_key flag (presence only) is preserved.
    return {
        k: v for k, v in config.items()
        if not (isinstance(v, str) and _is_secret_key(k))
    }


def update_search_config(api_key: str = None, **kwargs):
    """Merge non-secret search config into SEARCH_CONFIG.

    Provider API keys are intentionally NOT cached here. They are read on demand
    from settings/env via ``_get_provider_key`` (e.g. ``brave_search``), so the
    previous ``SEARCH_CONFIG["brave_api_key"] = api_key`` cache was never used
    for search and only leaked the decrypted key through ``get_search_config`` /
    ``GET /api/search/config`` (#1661). ``api_key`` is accepted for backward
    compatibility but no longer stored.
    """
    for k, v in kwargs.items():
        if not _is_secret_key(k):
            SEARCH_CONFIG[k] = v


def _site_scope(query: str) -> Tuple[str, Optional[str]]:
    """Split a `site:` operator off a query.

    Returns ``(query_without_operator, domain)``; ``domain`` is None when the
    query has no operator. `site:https://www.iso.org/standards` and
    `site:*.iso.org` both scope to their host.
    """
    rest, site = _extract_site_filter(query or "")
    if not site:
        return query, None
    domain = site.strip().lower()
    domain = re.sub(r"^[a-z][a-z0-9+.-]*://", "", domain)
    domain = domain.split("/", 1)[0].split("?", 1)[0].strip(".")
    if domain.startswith("*."):
        domain = domain[2:]
    return rest.strip(), domain or None


_CODE_HOSTS = {"github.com", "gitlab.com", "codeberg.org", "bitbucket.org"}
_CODE_HOST_RESERVED = {
    "issues", "pulls", "pull", "search", "topics", "orgs", "explore",
    "marketplace", "discussions", "notifications", "features", "sponsors",
}


def _site_path_segments(query: str) -> Tuple[Optional[str], List[str]]:
    rest, site = _extract_site_filter(query or "")
    if not site:
        return None, []
    bare = re.sub(r"^[a-z][a-z0-9+.-]*://", "", site.strip(), flags=re.I)
    host, _, path = bare.partition("/")
    host = host.split("?", 1)[0].strip(".").lower()
    if host.startswith("*."):
        host = host[2:]
    if host.startswith("www.") and host[4:] in _CODE_HOSTS:
        host = host[4:]
    path = path.split("?", 1)[0].split("#", 1)[0]
    return host or None, [s for s in path.split("/") if s]


def _site_path_prefix(query: str) -> Optional[str]:
    """The path a ``site:`` scope narrows to, casefolded, or None.

    On code hosts only the ``/<owner>/<repo>`` part counts (the model writes
    ``site:github.com/org/repo/issues`` but wants the repository);
    ``site:github.com/issues`` has no repository and yields None. Elsewhere
    the whole path is the prefix (``site:docs.python.org/3/library``).
    """
    host, segments = _site_path_segments(query)
    if not host or not segments:
        return None
    if host in _CODE_HOSTS:
        if len(segments) < 2 or segments[0].lower() in _CODE_HOST_RESERVED:
            return None
        return ("/" + "/".join(segments[:2])).casefold()
    return ("/" + "/".join(segments)).casefold()


def _repo_hint(query: str) -> str:
    """``owner/repo`` for a code-host ``site:`` scope, else ''."""
    host, segments = _site_path_segments(query)
    if host in _CODE_HOSTS and len(segments) >= 2 and segments[0].lower() not in _CODE_HOST_RESERVED:
        return f"{segments[0]}/{segments[1]}"
    return ""


def _url_on_site(url: str, domain: str) -> bool:
    """True when *url*'s host is *domain* or a subdomain of it."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return False
    return host == domain or host.endswith("." + domain)


def _keep_on_site(results: List[dict], domain: str, path_prefix: Optional[str] = None) -> List[dict]:
    """Rows on *domain*; rows under *path_prefix* (if any) are moved first."""
    on_site = [r for r in results if isinstance(r, dict) and _url_on_site(r.get("url", ""), domain)]
    if not path_prefix:
        return on_site
    inside = [r for r in on_site if _row_in_scope(r, domain, path_prefix)]
    return inside + [r for r in on_site if r not in inside]


def _hosts_summary(rows: List[dict], limit: int = 8) -> str:
    counts: Dict[str, int] = {}
    for row in rows:
        if isinstance(row, dict):
            host = _row_host(row) or "?"
            counts[host] = counts.get(host, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return ", ".join(f"{h}×{n}" if n > 1 else h for h, n in ordered)


# SearXNG merges 10-30 rows per page but used to be read five at a time, so a
# `site:github.com` query kept the one GitHub row among the first five and the
# relevance gate judged five rows where twenty were already paid for. The
# chain asks SearXNG for more rows (no extra upstream requests) and trims back
# to the caller's count after the site filter and relevance gate. Direct
# callers of _call_provider (/api/search/query, deep research) get their count.
_PROVIDER_POOL = {"searxng": 20}

# Providers that turn a `site:` operator into plain keywords themselves (see
# query.site_keyword_query); _call_provider does not retry them with keywords.
_SITE_AS_KEYWORDS_PROVIDERS = {"searxng"}

# A `site:` query that keeps fewer rows than this after the domain filter asks
# the provider for one more result page (see _add_second_page), if at least
# _SECOND_PAGE_MIN_SECONDS of the search deadline are left.
_SITE_MIN_ROWS = 3
_SECOND_PAGE_PROVIDERS = {"searxng"}
_SECOND_PAGE_MIN_SECONDS = 3.0


def _pool_size(provider_name: str, count: int) -> int:
    return max(count, _PROVIDER_POOL.get(provider_name, count))


def _call_provider(provider_name: str, query: str, count: int, time_filter: str = None) -> List[dict]:
    """Call a search provider by name, honouring a `site:` operator.

    Some engines behind SearXNG ignore `site:` and answer the remaining words
    instead — `site:lucide.dev license accessibility icons` came back as
    Missouri driver-licence pages, which were then fetched and handed to the
    model as if they were the icon library's licence. Off-domain results are
    dropped; when nothing on the domain remains, the provider is asked once
    more with the domain (and, for ``site:github.com/<owner>/<repo>/...``,
    the ``owner/repo``) as plain keywords. An empty list lets the provider
    chain fall through to a provider that does honour the operator.

    SearXNG already sends the operator as those keywords on its first
    request (``providers.searxng_search_api``), so it is not asked again.

    Returns at most *count* rows; ``_run_chain`` passes a larger pool
    (``_PROVIDER_POOL``) and trims after the relevance gate.
    """
    results = _call_provider_raw(provider_name, query, count, time_filter)
    rest, domain = _site_scope(query)
    if not domain or not results:
        return results
    path_prefix = _site_path_prefix(query)
    kept = _keep_on_site(results, domain, path_prefix)
    if provider_name in _SECOND_PAGE_PROVIDERS and len(kept) < _SITE_MIN_ROWS:
        kept, results = _add_second_page(
            provider_name, query, count, time_filter, domain, path_prefix, kept, results,
        )
    if kept:
        if len(kept) < len(results):
            off = [r for r in results if r not in kept]
            logger.info(
                "%s: dropped %d result(s) outside site:%s (%s)",
                provider_name, len(off), domain, _hosts_summary(off),
            )
        return kept
    if provider_name in _SITE_AS_KEYWORDS_PROVIDERS:
        logger.info(
            "%s: no result on %s for %r (%d off-domain results dropped: %s)",
            provider_name, domain, query, len(results), _hosts_summary(results),
        )
        return []
    repo = _repo_hint(query)
    alt_query = " ".join(part for part in (rest, repo, domain) if part).strip()
    if resilience.out_of_time():
        logger.info(
            "%s ignored the site: operator for %r; no time left to retry with %r",
            provider_name, query, alt_query,
        )
        return []
    logger.info(
        "%s ignored the site: operator for %r (%d off-domain results dropped: %s); "
        "retrying with %r",
        provider_name, query, len(results), _hosts_summary(results), alt_query,
    )
    return _keep_on_site(_call_provider_raw(provider_name, alt_query, count, time_filter), domain, path_prefix)


def _add_second_page(provider_name: str, query: str, count: int, time_filter: Optional[str],
                     domain: str, path_prefix: Optional[str], kept: List[dict],
                     results: List[dict]) -> Tuple[List[dict], List[dict]]:
    """Ask for result page 2 once when too few rows survived the site filter.

    Engines answer a ``site:`` query sent as keywords with mostly off-domain
    rows: on 2026-09-28, 5 of 7 yahoo rows were dropped for every
    ``site:docs.*`` lookup, leaving two. Page 2 costs one more request and
    usually carries more rows from the domain. Best effort: skipped when too
    little of the search deadline is left, and any failure keeps page 1.
    Returns ``(kept, results)`` with page 2 merged into both.
    """
    if resilience.out_of_time(_SECOND_PAGE_MIN_SECONDS):
        logger.info(
            "%s: %d row(s) on %s for %r; no time left for a second page",
            provider_name, len(kept), domain, query,
        )
        return kept, results
    try:
        with resilience.get_gate(provider_name).slot():
            page2 = searxng_search_api(query, count, time_filter=time_filter, pageno=2) or []
    except Exception as e:  # ProviderBusy, DeadlineExceeded, transport errors
        logger.info("%s: second page for %r skipped: %s", provider_name, query, e)
        return kept, results
    seen = {r.get("url") for r in results if isinstance(r, dict)}
    fresh = [r for r in page2 if isinstance(r, dict) and r.get("url") not in seen]
    added = _keep_on_site(fresh, domain, path_prefix)
    logger.info(
        "%s: only %d row(s) on %s from page 1 for %r; page 2 added %d of %d new row(s)",
        provider_name, len(kept), domain, query, len(added), len(fresh),
    )
    if not added:
        return kept, results + fresh
    merged = kept + added
    if path_prefix:
        inside = [r for r in merged if _row_in_scope(r, domain, path_prefix)]
        merged = inside + [r for r in merged if r not in inside]
    return merged, results + fresh


def _call_provider_raw(provider_name: str, query: str, count: int, time_filter: str = None) -> List[dict]:
    """Call a search provider by name. Returns list of results or empty list.

    Each call holds one of the provider's concurrency slots with paced starts
    (see ``resilience.ProviderGate``), so fifteen parallel research workers do
    not burst the upstream engines into 429s/CAPTCHAs. Raises
    ``resilience.ProviderBusy`` when no slot frees up in time.
    """
    with resilience.get_gate(provider_name).slot():
        return _dispatch_provider(provider_name, query, count, time_filter)


def _dispatch_provider(provider_name: str, query: str, count: int, time_filter: str = None) -> List[dict]:
    if provider_name == "searxng":
        return searxng_search_api(query, count, time_filter=time_filter)
    elif provider_name == GITHUB_ISSUES:
        return github_issue_search(query, count, time_filter)
    elif provider_name == "brave":
        return brave_search(query, count, time_filter)
    elif provider_name == "duckduckgo":
        return duckduckgo_search(query, count, time_filter)
    elif provider_name == "google_pse":
        return google_pse_search(query, count, time_filter)
    elif provider_name == "tavily":
        return tavily_search(query, count, time_filter)
    elif provider_name == "serper":
        return serper_search(query, count, time_filter)
    return []


# If the self-hosted SearXNG instance is up but all enabled engines return
# empty, fall back to the no-key provider so "search X" still works on fresh
# installs. Users can override/disable with `search_fallback_chain`.
_FALLBACK_ORDER = ["duckduckgo"]
# `search_fallback_chain: ["none"]` means no fallback at all. An empty chain
# keeps meaning "the default chain" above, so an install that never touched
# the setting keeps its DuckDuckGo safety net; before 2026-09-28 removing the
# last fallback in Settings saved [] and silently brought DuckDuckGo back.
# ("disabled" entries are skipped too, which older saves relied on.)
FALLBACK_NONE = "none"
# Names older builds stored for a provider the dispatcher knows by another.
# The Research tab saved "google", which matched no provider, so Deep Research
# silently used the fallback engine instead of Google PSE (2026-09-28).
_PROVIDER_ALIASES = {"google": "google_pse"}


def normalize_provider_name(name: Any) -> str:
    """The dispatcher's name for a stored provider choice ("" when unset)."""
    text = str(name or "").strip().lower()
    return _PROVIDER_ALIASES.get(text, text)


def _build_provider_chain(primary: str) -> List[str]:
    """Build ordered list: primary first, then configured/default fallbacks."""
    primary = normalize_provider_name(primary) or primary
    chain = [primary]
    settings = _get_search_settings()
    user_chain = settings.get("search_fallback_chain") or []
    if isinstance(user_chain, str):
        user_chain = [s.strip() for s in user_chain.split(",") if s.strip()]
    user_chain = [normalize_provider_name(fb) for fb in user_chain]
    if FALLBACK_NONE in user_chain:
        return chain
    fallbacks = user_chain if user_chain else _FALLBACK_ORDER
    for fb in fallbacks:
        if fb and fb != primary and fb not in chain and fb != "disabled":
            chain.append(fb)
    return chain


def _run_chain(query: str, count: int, time_filter: Optional[str], chain: List[str],
               label: str = "") -> Tuple[List[dict], Dict[str, str]]:
    """Ask each provider in order until one returns relevant rows.

    Returns ``(results, attempts)`` where attempts maps provider to
    ``"ok (n)"``, ``"empty"``, ``"irrelevant (0/n)"`` (rows came back but
    the relevance gate rejected all of them), ``"cooling down"``, ``"busy"``
    or ``"error: ..."``. Provider implementations own their retries: an
    empty SearXNG answer followed by a DuckDuckGo fallback costs one call
    each. A provider whose circuit breaker is open is skipped without
    waiting. At most *count* rows are returned.

    Under a search deadline (``resilience.deadline_scope``) providers are not
    started once too little time is left (``"out of time"``), and a provider
    that runs out mid-call is reported the same way.

    When the GitHub issue step answers only with ``low_signal`` rows (a
    search across all of GitHub that found no issue with comments or
    reactions), the chain goes on to the web providers and merges their rows
    with GitHub's (``_merge_with_github``). If none answers, the GitHub rows
    are returned alone.
    """
    attempts: Dict[str, str] = {}
    held_github: List[dict] = []
    for provider_name in chain:
        key = f"{provider_name}{label}"
        if resilience.out_of_time():
            attempts[key] = OUT_OF_TIME
            logger.info("Search time limit reached; not asking %s for %r", provider_name, query)
            continue
        breaker = resilience.get_breaker(provider_name)
        if not breaker.allow():
            attempts[key] = "cooling down"
            logger.debug("Skipping %s: circuit open for another %.0fs", provider_name, breaker.remaining())
            continue
        results: List[dict] = []
        try:
            logger.info(f"Attempting {provider_name} search")
            results = _call_provider(provider_name, query, _pool_size(provider_name, count), time_filter)
        except resilience.DeadlineExceeded as e:
            attempts[key] = OUT_OF_TIME
            logger.info("%s stopped for %r: %s", provider_name, query, e)
            continue
        except resilience.ProviderBusy as e:
            attempts[key] = "busy"
            # The GitHub step is an optional shortcut; being busy is normal.
            (logger.info if provider_name == GITHUB_ISSUES else logger.warning)(str(e))
            continue
        except (NetworkError, ParseError, RateLimitError) as e:
            error_logger.error(f"{provider_name} search error: {e}")
            attempts[key] = f"error: {e}"
            continue
        except Exception as e:
            error_logger.error(f"Unexpected error during {provider_name} search: {e}")
            attempts[key] = f"error: {e}"
            continue
        finally:
            # A half-open probe that ended without a verdict (the provider
            # does not report transport outcomes, or answered honestly
            # empty) must not keep the breaker waiting on it forever.
            breaker.release_probe()
        raw_count = len(results) if results else 0
        if results and provider_name != GITHUB_ISSUES:
            # GitHub's issue search ANDs every term itself; its rows are
            # matches by construction and their bodies are cut to a snippet.
            results = _keep_relevant_results(query, results)
        if results:
            results = results[:count]
            if provider_name == GITHUB_ISSUES and all(
                isinstance(r, dict) and r.get("low_signal") for r in results
            ):
                # Only issues nobody commented on or reacted to, usually in
                # tiny repositories: weak evidence on their own. Keep them and
                # ask the web providers as well.
                held_github = results
                attempts[key] = f"ok ({len(results)}, no engagement)"
                logger.info(
                    "GitHub issue search found only issues without comments or reactions for %r; "
                    "adding web results", query,
                )
                continue
            attempts[key] = f"ok ({len(results)})"
            logger.info(f"{provider_name} search returned {len(results)} relevant results")
            if held_github:
                results = _merge_with_github(results, held_github, count)
            return results, attempts
        attempts[key] = f"irrelevant (0/{raw_count})" if raw_count else "empty"
    if held_github:
        return held_github, attempts
    return [], attempts


def _merge_with_github(web_rows: List[dict], github_rows: List[dict], count: int) -> List[dict]:
    """Web rows plus some low-engagement GitHub rows, at most *count* in all.

    The GitHub rows get up to half the slots (at least one) and any slots the
    web rows leave empty. The mixed list then goes through the generic
    ranking (``_rank_rows``).
    """
    web_slots = max(0, count - min(len(github_rows), max(1, count // 2)))
    merged: List[dict] = []
    seen = set()
    for row in web_rows[:web_slots] + github_rows + web_rows[web_slots:]:
        if len(merged) >= count:
            break
        if row.get("url") in seen:
            continue
        seen.add(row.get("url"))
        merged.append(row)
    github_kept = [r for r in merged if isinstance(r, dict) and r.get("engine") == "github"]
    logger.info(
        "Merged %d web row(s) with %d of %d low-engagement GitHub row(s) (top GitHub: %s)",
        len(merged) - len(github_kept), len(github_kept), len(github_rows),
        github_row_summary(github_kept[0]) if github_kept else "none",
    )
    return merged


def _is_empty_attempt(outcome: str) -> bool:
    return outcome == "empty" or outcome.startswith("irrelevant")


def _rank_rows(query: str, results: List[dict]) -> List[dict]:
    """Generic ranking, except for GitHub issue rows.

    Those arrive already ordered by ``providers.rank_github_issues`` (title and
    body match, state, recency, engagement); the generic title/domain/age
    ranking would undo that, since every row shares a domain and its title
    carries the "· Issue #N · owner/repo (state)" label.
    """
    if results and all(isinstance(r, dict) and r.get("engine") == "github" for r in results):
        return results
    return rank_search_results(query, results)


def _with_github_step(query: str, chain: List[str]) -> List[str]:
    """Put the GitHub issue search in front of the chain when it applies."""
    if GITHUB_ISSUES in chain or not github_issue_search_enabled():
        return chain
    if not github_issue_scope(query):
        return chain
    if resilience.cooldowns.is_cooling("github issue search"):
        return chain
    return [GITHUB_ISSUES] + list(chain)


def _search_with_fallbacks(query: str, count: int, time_filter: Optional[str],
                           chain: List[str]) -> Tuple[List[dict], Dict[str, str], str]:
    """Run the provider chain, then once more with a simplified query if empty.

    GitHub-issue-scoped queries (``site:github.com/<org>/<repo>/issues ...``,
    ``GitHub issue ...``) ask GitHub's issue search first; only the first
    pass does, since that step already retries with fewer terms itself.
    Identical ``(query, count, time_filter, chain)`` requests share one
    outcome for a few minutes (single-flight: parallel workers asking the
    same thing wait for the first instead of all hitting the providers).
    Returns ``(results, attempts, query_that_answered)``.
    """
    def compute():
        first_chain = _with_github_step(query, chain)
        results, attempts = _run_chain(query, count, time_filter, first_chain)
        used = query
        if not results:
            simplified = simplify_query(query)
            if not simplified or simplified.casefold() == (query or "").strip().casefold():
                pass  # nothing shorter to try
            elif resilience.out_of_time():
                attempts["[simplified]"] = OUT_OF_TIME
                logger.info("Search time limit reached; not retrying %r as %r", query, simplified)
            else:
                logger.info(
                    "No relevant results for %r; retrying with simplified query %r",
                    query, simplified,
                )
                results, retry_attempts = _run_chain(
                    simplified, count, time_filter,
                    [p for p in chain if p != GITHUB_ISSUES], label="[simplified]",
                )
                attempts.update(retry_attempts)
                used = simplified
        return results, attempts, used

    def ttl_for(outcome) -> float:
        results, attempts, _ = outcome
        if results:
            return resilience.RESULT_TTL_HIT
        # Only an honest "nothing found" is worth repeating; an outcome shaped
        # by errors, a busy provider or an open breaker should be retried.
        if attempts and all(_is_empty_attempt(v) for v in attempts.values()):
            return resilience.RESULT_TTL_EMPTY
        return 0.0

    key = ((query or "").strip(), count, time_filter, tuple(chain))
    try:
        (results, attempts, used), hit = resilience.search_results_cache.get_or_compute(key, compute, ttl_for)
    except resilience.DeadlineExceeded as e:
        # Another worker is still running this exact search and our own
        # budget ran out waiting for it.
        logger.info("Search for %r: %s", query, e)
        return [], {"search": OUT_OF_TIME}, query
    if hit:
        logger.info("Search result cache hit for %r (%d results)", query, len(results))
    # Callers rank/annotate rows in place; never hand out the cached dicts.
    return [dict(r) for r in results], dict(attempts), used


# ----------------------------------------------------------------------
# Unified search with caching and retry
# ----------------------------------------------------------------------
def searxng_search_results(query: str, count: int = 10, time_filter: str = None,
                           deadline_seconds: Optional[float] = None) -> list[dict]:
    """Perform a web search using configured provider with caching and retry.

    The whole call runs under one search deadline (``deadline_seconds``,
    default ``ODYSSEUS_SEARCH_DEADLINE_SECONDS`` = 30 s).
    """
    with resilience.deadline_scope(deadline_seconds) as deadline:
        return resilience.run_within_deadline(
            lambda: _searxng_search_results(query, count, time_filter), deadline, [],
            what=f"search for {query!r}",
        )


def _searxng_search_results(query: str, count: int, time_filter: Optional[str]) -> list[dict]:
    settings = _get_search_settings()
    search_provider = settings.get("search_provider", "searxng")
    result_count = _get_result_count()
    # Use configured count if caller used default
    if count == 10:
        count = result_count
    query, site_note = normalize_site_wildcard(query)
    if site_note:
        logger.info("Search rewritten to %r: %s", query, site_note)

    cache_key = generate_cache_key(f"{query}|{count}|{time_filter}")
    cache_file = SEARCH_CACHE_DIR / f"{cache_key}.cache"

    # Check cache
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cached_data = json.load(f)
            expiry_raw = cached_data.get("expiry")
            expiry = datetime.fromisoformat(expiry_raw) if expiry_raw else None
            if expiry and datetime.now() < expiry:
                logger.debug(f"Search cache hit for query: {query}")
                results = cached_data["data"]
                _record_query(query, bool(results), cache_hit=True)
                return results
            else:
                cache_file.unlink(missing_ok=True)
                search_cache_index.pop(cache_key, None)
        except Exception as e:
            logger.warning(f"Failed to read search cache for {query}: {e}")
            cache_file.unlink(missing_ok=True)
            search_cache_index.pop(cache_key, None)

    logger.debug(f"Search cache miss for query: {query}")

    if search_provider == "disabled":
        logger.info("Search is disabled via admin settings")
        return []

    provider_chain = _build_provider_chain(search_provider)

    results, _attempts, _used_query = _search_with_fallbacks(query, count, time_filter, provider_chain)

    success = bool(results)
    _record_query(query, success, cache_hit=False)

    if success:
        results = _rank_rows(query, results)
        try:
            expiry = datetime.now() + _cache_duration_for_query(query)
            cache_data = {
                "timestamp": datetime.now().isoformat(),
                "expiry": expiry.isoformat(),
                "data": results,
            }
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f)
            search_cache_index[cache_key] = datetime.now()
            cleanup_cache(SEARCH_CACHE_DIR, search_cache_index, timedelta(hours=1))
        except Exception as e:
            logger.warning(f"Failed to write search cache for {query}: {e}")

    if not success:
        logger.error(f"All search providers failed for query: {query}")

    return results


# ----------------------------------------------------------------------
# Cache invalidation
# ----------------------------------------------------------------------
def invalidate_search_cache(query: Optional[str] = None) -> None:
    """Invalidate cached search results. None clears all, otherwise just the given query."""
    if query is None:
        for file in SEARCH_CACHE_DIR.glob("*.cache"):
            try:
                file.unlink(missing_ok=True)
            except Exception as e:
                error_logger.warning(f"Failed to delete cache file {file}: {e}")
        search_cache_index.clear()
        logger.info("All search cache entries have been cleared.")
    else:
        # Match the key the write path stores: searxng_search_results replaces
        # the caller's default count with the configured _get_result_count()
        # (default 5), so a hardcoded "|10|None" never matched a real entry.
        cache_key = generate_cache_key(f"{query}|{_get_result_count()}|None")
        cache_file = SEARCH_CACHE_DIR / f"{cache_key}.cache"
        if cache_file.exists():
            try:
                cache_file.unlink(missing_ok=True)
                search_cache_index.pop(cache_key, None)
                logger.info(f"Cache entry for query '{query}' has been invalidated.")
            except Exception as e:
                error_logger.warning(f"Failed to delete cache file for query '{query}': {e}")
        else:
            logger.info(f"No cache entry found for query '{query}'.")


# ----------------------------------------------------------------------
# Comprehensive web search (with advanced filtering)
# ----------------------------------------------------------------------
def comprehensive_web_search(
    query: str,
    max_pages: int = 3,
    max_workers: int = 4,
    time_filter: str = None,
    domain_whitelist: Optional[Set[str]] = None,
    domain_blacklist: Optional[Set[str]] = None,
    content_type: Optional[str] = None,
    language: Optional[str] = None,
    min_content_length: int = 0,
    return_sources: bool = False,
    deadline_seconds: Optional[float] = None,
):
    """Perform comprehensive web search with content fetching and advanced filtering.

    The whole call -- provider chain with its fallbacks, retries and the
    simplified re-query, then the page fetches -- shares one deadline:
    ``deadline_seconds``, default ``ODYSSEUS_SEARCH_DEADLINE_SECONDS`` (30 s).
    The provider chain may use all but a reserve kept for fetching pages
    (30% of the budget, at most 8 s). When the time runs out the search
    returns what it has: rows found so far with the pages fetched so far,
    or a message saying the time limit was reached.
    """
    with resilience.deadline_scope(deadline_seconds) as deadline:
        return _comprehensive_web_search(
            query, max_pages, max_workers, time_filter, domain_whitelist, domain_blacklist,
            content_type, language, min_content_length, return_sources, deadline,
        )


def _comprehensive_web_search(query, max_pages, max_workers, time_filter, domain_whitelist,
                              domain_blacklist, content_type, language, min_content_length,
                              return_sources, deadline):
    logger.info(f"Starting comprehensive search for: {query}")
    if time_filter:
        logger.info(f"Applying time filter: {time_filter}")
    query, site_note = normalize_site_wildcard(query)
    if site_note:
        logger.info("Search rewritten to %r: %s", query, site_note)

    settings = _get_search_settings()
    search_provider = settings.get("search_provider", "searxng")
    result_count = _get_result_count()

    if search_provider == "disabled":
        logger.info("Search is disabled via admin settings")
        msg = "Web search is disabled by the administrator."
        return (msg, []) if return_sources else msg

    # Use configured result count (at least max_pages for content fetching)
    fetch_count = max(result_count, max_pages)

    provider_chain = _build_provider_chain(search_provider)

    reserve = min(_FETCH_RESERVE_MAX, deadline.seconds * _FETCH_RESERVE_SHARE)
    with resilience.deadline_scope(max(0.0, deadline.remaining() - reserve)) as search_deadline:
        search_results, provider_attempts, _used_query = resilience.run_within_deadline(
            lambda: _search_with_fallbacks(query, fetch_count, time_filter, provider_chain),
            search_deadline,
            lambda: ([], {"search": OUT_OF_TIME}, query),
            what=f"provider chain for {query!r}",
        )

    if not search_results:
        tally = ", ".join(f"{p}:{r}" for p, r in provider_attempts.items()) or "no providers configured"
        any_errors = any(
            r.startswith(("error", "busy", "cooling down"))
            for p, r in provider_attempts.items()
            # The GitHub issue step is an optional shortcut; its being busy
            # says nothing about whether web search works.
            if not p.startswith(GITHUB_ISSUES)
        )
        any_irrelevant = any(r.startswith("irrelevant") for r in provider_attempts.values())
        timed_out = any(r == OUT_OF_TIME for r in provider_attempts.values())
        if timed_out:
            msg = (
                f"Web search stopped at its {deadline.seconds:.0f}s time limit before any provider "
                f"returned relevant results. Tried: {tally}. The search providers are slow or "
                "rate-limited right now; try again shortly, with fewer and more specific terms"
            )
        elif any_errors:
            msg = f"Web search failed — all providers errored or returned empty. Tried: {tally}"
            if any(r == "cooling down" for r in provider_attempts.values()):
                msg += (
                    ". A provider is paused after repeated timeouts and resumes on its own "
                    "within a couple of minutes"
                )
        else:
            msg = (
                f"No search results found. Tried: {tally}. "
                "All providers returned empty — possibly a niche query or upstream rate-limiting; "
                "rephrasing or using the browser tool for a specific URL may help."
            )
        if any_irrelevant:
            # Engines answered, but with rows about other things. Long
            # model-written keyword lists are the usual cause: web engines
            # match one or two of the words and return dictionary/SEO pages.
            msg += (
                " The search engine did answer, but none of its rows mentioned enough of the "
                "query's terms. Search again with 3-6 specific terms (a product, project, error "
                "or identifier name plus one or two topic words) rather than a long keyword list."
            )
        _, _site_domain = _site_scope(query)
        if _site_domain:
            msg += (
                f" The query was scoped with site:{_site_domain} and no result from that domain "
                "came back — off-domain hits are never substituted. Fetch a page on the site "
                "directly with web_fetch, or search again without the site: operator."
            )
        if site_note:
            msg = f"Note: {site_note}\n{msg}"
        logger.warning(msg)
        return (msg, []) if return_sources else msg

    search_results = _rank_rows(query, search_results)

    # URL filter helper
    def url_passes_filters(url: str) -> bool:
        try:
            netloc = urlparse(url).netloc.lower()
        except Exception:
            return False
        if domain_whitelist is not None and netloc not in domain_whitelist:
            return False
        if domain_blacklist is not None and netloc in domain_blacklist:
            return False
        if content_type:
            ct = content_type.lower()
            if ct == "article":
                if not any(k in url.lower() for k in ("article", "blog", "news", "post")):
                    return False
            elif ct == "forum":
                if not any(k in url.lower() for k in ("forum", "discussion", "thread", "topic")):
                    return False
            elif ct == "academic":
                if not any(k in url.lower() for k in ("pdf", "doi", "scholar", "arxiv", "journal", "research")):
                    return False
        if language:
            lang_pat = language.lower()
            if not (f"/{lang_pat}/" in url.lower() or f"?lang={lang_pat}" in url.lower() or f"&lang={lang_pat}" in url.lower()):
                return False
        return True

    filtered_urls = [r["url"] for r in search_results[:max_pages] if url_passes_filters(r["url"])]
    if not filtered_urls:
        logger.warning("All URLs filtered out by advanced criteria")
        msg = "No suitable results after applying filters."
        return (msg, []) if return_sources else msg

    # Build sources list for the frontend (before content fetching)
    _source_list = [
        {"url": r.get("url", ""), "title": r.get("title", "")}
        for r in search_results if r.get("url")
    ]

    # Map each URL to its [i] number in the sources list so fetched content
    # blocks can be labeled with the SAME index the model cites.
    _url_index = {
        r["url"]: i for i, r in enumerate(search_results, 1) if r.get("url")
    }

    # Fetch content in parallel, within what is left of the deadline. Pages
    # still loading when it runs out are left out (the search rows stay).
    fetched_content = []
    unfetched = 0
    fetch_budget = deadline.remaining()
    if fetch_budget < resilience.MIN_ATTEMPT_SECONDS:
        unfetched = len(filtered_urls)
        logger.info("Search time limit reached; not fetching %d page(s) for %r", unfetched, query)
    else:
        page_timeout = max(1, int(min(_PAGE_FETCH_TIMEOUT, fetch_budget)))
        executor = ThreadPoolExecutor(max_workers=max_workers)
        future_to_url = {
            executor.submit(fetch_webpage_content, url, page_timeout, retry_attempt=0): url
            for url in filtered_urls
        }
        try:
            for future in as_completed(future_to_url, timeout=fetch_budget):
                url = future_to_url[future]
                try:
                    result = future.result()
                    if result["success"] and result["content"] and len(result["content"]) >= min_content_length:
                        # Remember which source this fetch belongs to: redirects
                        # can change result["url"] and completion order is
                        # arbitrary, so the block label cannot be recomputed later.
                        result["source_index"] = _url_index.get(url)
                        fetched_content.append(result)
                except Exception as e:
                    logger.error(f"Exception while fetching {url}: {str(e)}")
        except FuturesTimeout:
            unfetched = sum(1 for f in future_to_url if not f.done())
            logger.info(
                "Search time limit reached with %d page fetch(es) still running for %r; "
                "returning without them", unfetched, query,
            )
        finally:
            # Do not wait for stragglers; each is bounded by its own timeout.
            executor.shutdown(wait=False, cancel_futures=True)

    logger.info(f"Successfully fetched content from {len(fetched_content)} pages")

    # Format results
    output_parts = []

    if search_results:
        output_parts.append("```sources")
        for i, result in enumerate(search_results, 1):
            output_parts.append(f"[{i}] {result['title']}")
            output_parts.append(f"    {result['url']}")
            if result.get("age"):
                output_parts.append(f"    {result['age']}")
        output_parts.append("```")
        output_parts.append("")

    output_parts.append("=" * 70)
    output_parts.append("WEB SEARCH RESULTS AND FETCHED CONTENT")
    output_parts.append(f"Query: {query}")
    if site_note:
        output_parts.append(f"Note: {site_note}")
    output_parts.append(f"Searched {len(search_results)} results, fetched {len(fetched_content)} pages")
    if unfetched:
        output_parts.append(
            f"Note: the {deadline.seconds:.0f}s search time limit was reached; "
            f"{unfetched} page(s) were not fetched. Use web_fetch on a source URL to read it."
        )
    output_parts.append("=" * 70)
    output_parts.append("")

    output_parts.append("SEARCH RESULTS SUMMARY:")
    output_parts.append("-" * 50)
    for i, result in enumerate(search_results, 1):
        output_parts.append(f"\n[{i}] {result['title']}")
        output_parts.append(f"    URL: {result['url']}")
        output_parts.append(f"    Snippet: {result['snippet'][:200]}...")
        if result.get("age"):
            output_parts.append(f"    Age: {result['age']}")

    if fetched_content:
        output_parts.append("\n" + "=" * 70)
        output_parts.append("FETCHED PAGE CONTENT:")
        output_parts.append("-" * 50)

        # Emit blocks in source order, numbered with the same [i] as the
        # sources list, so [CONTENT 2] really is content from source [2].
        # Before this, blocks were numbered 1..N in fetch COMPLETION order,
        # which matched neither the sources list nor each other run to run.
        fetched_content.sort(key=lambda c: c.get("source_index") or len(search_results) + 1)
        for content in fetched_content:
            _idx = content.get("source_index")
            _label = f"[CONTENT {_idx}]" if _idx else "[CONTENT]"
            output_parts.append(f"\n{_label} From: {content['url']}")
            output_parts.append(f"Title: {content['title']}")
            output_parts.append("-" * 30)

            text = content["content"][:3000]
            if len(content["content"]) > 3000:
                text += "... [truncated]"
            output_parts.append(text)

            key_points = extract_key_points(content["content"])
            if key_points:
                output_parts.append("\nKey Points:")
                for pt in key_points[:5]:
                    output_parts.append(f"- {pt}")

            tldr = get_tldr(content["content"])
            if tldr:
                output_parts.append("\nTL;DR:")
                output_parts.append(tldr)

            quotes = extract_quotes(content["content"])
            if quotes:
                output_parts.append("\nImportant Quotes:")
                for q in quotes[:3]:
                    output_parts.append(f"\u201c{q}\u201d")

            stats = extract_statistics(content["content"])
            if stats:
                output_parts.append("\nData / Statistics:")
                for s in stats[:5]:
                    output_parts.append(f"- {s}")

            output_parts.append("")

    output_parts.append("=" * 70)
    output_parts.append("END OF WEB SEARCH RESULTS")
    output_parts.append("=" * 70)

    instructions = (
        "\n\nIMPORTANT INSTRUCTIONS:\n"
        "1. Use the above web search results and fetched content to answer the user's question\n"
        "2. Prioritize information from the FETCHED PAGE CONTENT section as it contains actual page data\n"
        "3. Cross-reference multiple sources when possible\n"
        "4. If the information is time-sensitive, pay attention to the age of the results\n"
        "5. Be explicit if the search results don't contain sufficient information to fully answer the question"
    )
    output_parts.append(instructions)

    result = "\n".join(output_parts)
    return (result, _source_list) if return_sources else result
