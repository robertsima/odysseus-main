"""Search provider implementations: SearXNG, Brave, DuckDuckGo, Google PSE, Tavily, Serper."""

import json
import logging
import os
import re
from typing import List, Optional
from urllib.parse import urljoin, urlparse, parse_qs

import httpx
from bs4 import BeautifulSoup

from src.constants import SEARXNG_INSTANCE, REQUEST_TIMEOUT, WEB_FETCH_USER_AGENT
from .analytics import RateLimitError, error_logger
from .query import build_enhanced_query, site_keyword_query
from .resilience import DeadlineExceeded, out_of_time, request_timeout

logger = logging.getLogger(__name__)

# Provider registry — maps setting value to (label, needs_key, needs_url)
PROVIDER_INFO = {
    "searxng":  ("SearXNG",           False, True),
    "brave":    ("Brave Search",      True,  False),
    "duckduckgo": ("DuckDuckGo",      False, False),
    "google_pse": ("Google PSE",      True,  False),
    "tavily":   ("Tavily",            True,  False),
    "serper":   ("Serper",            True,  False),
    "disabled": ("Disabled",          False, False),
}


# ── Settings helpers ──

def _get_search_settings() -> dict:
    """Return search settings from admin config, falling back to env defaults."""
    try:
        from src.settings import load_settings
        return load_settings()
    except Exception:
        return {}


def _get_search_instance() -> str:
    """Return the active search API URL from admin settings, falling back to env var."""
    settings = _get_search_settings()
    url = (settings.get("search_url") or "").strip()
    if url:
        return url.rstrip("/")
    return SEARXNG_INSTANCE


def _get_provider_key(provider: str) -> str:
    """Return the API key for a specific provider, with legacy fallback."""
    settings = _get_search_settings()
    key_map = {
        "brave": "brave_api_key",
        "google_pse": "google_pse_key",
        "tavily": "tavily_api_key",
        "serper": "serper_api_key",
    }
    field = key_map.get(provider, "")
    if field:
        val = (settings.get(field) or "").strip()
        if val:
            return val
    # Legacy fallback: old shared search_api_key field
    legacy = (settings.get("search_api_key") or "").strip()
    if legacy:
        return legacy
    env_map = {
        "brave": "DATA_BRAVE_API_KEY",
        "google_pse": "GOOGLE_API_KEY",
        "tavily": "TAVILY_API_KEY",
        "serper": "SERPER_API_KEY",
    }
    env_name = env_map.get(provider, "")
    return (os.environ.get(env_name) or "").strip() if env_name else ""


def _get_result_count() -> int:
    """Return configured result count, default 5."""
    settings = _get_search_settings()
    try:
        return int(settings.get("search_result_count", 5))
    except (ValueError, TypeError):
        return 5


# Canonical SafeSearch levels: "strict" (default), "moderate", "off".
# Each provider has its own knob name and value space -- see _safesearch_for(...).
_SAFESEARCH_LEVELS = ("strict", "moderate", "off")


def _get_safesearch_level() -> str:
    """Return configured SafeSearch level normalized to a canonical value."""
    settings = _get_search_settings()
    raw = (settings.get("search_safesearch") or "strict").strip().lower()
    if raw in _SAFESEARCH_LEVELS:
        return raw
    aliases = {
        "on": "strict", "high": "strict", "2": "strict",
        "medium": "moderate", "1": "moderate", "default": "moderate",
        "none": "off", "disabled": "off", "0": "off",
    }
    return aliases.get(raw, "strict")


def _safesearch_for(provider: str) -> Optional[str]:
    """Translate the canonical SafeSearch level into provider-specific values."""
    level = _get_safesearch_level()
    if provider == "searxng":
        return {"strict": "2", "moderate": "1", "off": "0"}[level]
    if provider == "brave":
        return level
    if provider == "duckduckgo_lib":
        return {"strict": "on", "moderate": "moderate", "off": "off"}[level]
    if provider == "duckduckgo_html":
        return {"strict": "1", "moderate": "-1", "off": "-2"}[level]
    if provider == "google_pse":
        return None if level == "off" else "active"
    if provider == "serper":
        return None if level == "off" else "active"
    return None


# ── SearXNG ──

_NEWS_HINTS = ("news", "nyheter", "headlines", "breaking", "latest", "today", "idag")

# SearXNG's default general engines (brave, duckduckgo, google cse, wikipedia,
# wikidata on the 2026.9.25 image) are rate-limited from a home IP under
# research load: in the 2026-09-27 22:30-22:55 logs brave reported "too many
# requests" on 365 of 366 requests, duckduckgo timed out on 365 and google cse
# was suspended on 346. Pin engines that answer instead. Override with
# SEARXNG_GENERAL_ENGINES (comma-separated SearXNG engine names; an empty value
# sends no pin and lets SearXNG use its enabled general engines).
#
# Default pin: yahoo, brave, wikipedia (wikipedia only on short entity-like
# queries, see _ENTITY_ONLY_ENGINES). Checked against searx/settings.yml and
# searx/engines/ at searxng 12f8b6515 (the 2026.9.25 image the NAS runs).
# Evidence: the 2026-09-28 02:42-03:31 bundle (21 searches, pin
# yahoo,qwant,wikipedia) and the 2026-09-27 22:55 bundle (pin bing,yahoo).
#
# * yahoo  -- ``disabled: true`` (answers when named), scrapes search.yahoo.com
#   with no key. It answered from the NAS in both bundles (21/32 calls with
#   rows on 2026-09-27; 7 rows per ask on 2026-09-28). It returned 0 rows for
#   a ``site:github.com/<owner>/<repo>`` query, so searxng_search_api sends
#   the operator as plain keywords (query.site_keyword_query).
# * brave  -- enabled by default, scrapes search.brave.com with no key
#   (``require_api_key: False``). When the 2026-09-28 pin came back empty,
#   SearXNG's defaults ran and brave returned 20 rows, 14 of which passed the
#   relevance gate. On 2026-09-27 it reported "too many requests" on 365 of
#   366 requests, but those were five research workers sending every query
#   to every default engine at once. Now it gets one paced request per
#   search, and when it reports "too many requests" or a CAPTCHA the
#   cooldown below leaves it out for SEARXNG_ENGINE_COOLDOWN_SECONDS while
#   yahoo carries on.
# * wikipedia -- enabled by default. It makes one REST summary lookup by
#   exact title (the query, title-cased), so it can only answer a query that
#   *is* an article title ("pgvector", "Vector database"). With the default
#   ``display_type: ["infobox"]`` the hit arrives in the JSON ``infoboxes``
#   list, not in ``results``. That is why it showed "0 rows" on all 3 asks on
#   2026-09-28. searxng_search_api now reads infoboxes too (_infobox_rows).
#   It is only asked for short entity-like queries: a 10-word keyword list is
#   never a title.
#
# Left out, and why:
# * qwant -- ``disabled: true``, keyless JSON API. Its first reply from the NAS
#   on 2026-09-28 was a CAPTCHA (cooled down 300 s), so from a residential IP
#   it answers nothing. Operators on other IPs can add it back through
#   SEARXNG_GENERAL_ENGINES.
# * bing -- answered 32/32 calls on 2026-09-27, but the relevance gate
#   dropped nearly all of its rows (junk single-word matches).
# * duckduckgo, google cse -- CAPTCHA, timeouts or "too many requests" from
#   the NAS in both bundles. google (``disabled: true``) serves CAPTCHAs to
#   scrapers from a residential IP under parallel load.
# * startpage, mojeek, dogpile, marginalia, yahoo news -- ``inactive: true``
#   on this image, so SearXNG does not load them. presearch no longer exists.
#   See _UNAVAILABLE_ENGINES.
# * mwmbl, yep, wiby -- small indexes. baidu, naver, seznam, yandex, sogou,
#   360search, quark -- regional engines.
#
# The pin only works without a ``categories`` parameter: SearXNG *adds* every
# enabled engine of a requested category to an explicit ``engines`` list
# (searx/webadapter.py parse_generic), which is how the old
# ``engines=bing,mojeek,presearch&categories=general`` request reached all of
# the rate-limited defaults on every query. mojeek is inactive and presearch
# no longer exists on current images, so that pin was really "bing + defaults".
_DEFAULT_GENERAL_ENGINES = "yahoo,brave,wikipedia"

# Engines that look up one article by exact title. They are asked only when
# the query could be a title (at most _ENTITY_MAX_WORDS words, no site:
# scope). They do not count as a working pin by themselves: when every other
# pinned engine is cooling down, SearXNG's defaults are used instead.
_ENTITY_ONLY_ENGINES = frozenset({"wikipedia", "wikidata"})
_ENTITY_MAX_WORDS = 3

# Engine names that cannot answer on searxng 2026.9.25: ``inactive: true``
# engines are not loaded at all and presearch was removed. Naming one in the
# pin changes nothing except hiding that the pin is shorter than it looks, so
# it is dropped with a warning (once per name).
_UNAVAILABLE_ENGINES = frozenset({
    "mojeek", "presearch", "startpage", "startpage news", "dogpile",
    "marginalia", "yahoo news",
})
_warned_unavailable: set = set()

# Engines that SearXNG reports as blocked are left out of the pin for this
# long (SEARXNG_ENGINE_COOLDOWN_SECONDS). SearXNG suspends them itself, but a
# request pinned only to suspended engines is a wasted round trip that then
# pays for two retries.
_ENGINE_BLOCK_MARKERS = (
    "suspended", "too many requests", "captcha", "access denied", "forbidden",
    "403", "429",
)


def _general_engines() -> List[str]:
    """Engines to pin for general searches, read at call time."""
    raw = os.environ.get("SEARXNG_GENERAL_ENGINES")
    if raw is None:
        raw = _DEFAULT_GENERAL_ENGINES
    names: List[str] = []
    for name in raw.split(","):
        name = name.strip()
        if not name or name in names:
            continue
        if name.casefold() in _UNAVAILABLE_ENGINES:
            if name.casefold() not in _warned_unavailable:
                _warned_unavailable.add(name.casefold())
                logger.warning(
                    "SEARXNG_GENERAL_ENGINES names %r, which is inactive or removed on "
                    "searxng 2026.9.25; leaving it out of the pin", name,
                )
            continue
        names.append(name)
    return names


def _engine_cooldown_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("SEARXNG_ENGINE_COOLDOWN_SECONDS", "") or 300))
    except ValueError:
        return 300.0


def _engine_cooldown_key(engine: str) -> str:
    return f"searxng engine {engine}"


def _entity_like(query: Optional[str]) -> bool:
    """True when *query* could be an encyclopedia article title.

    At most ``_ENTITY_MAX_WORDS`` words, no search operators. ``pgvector``,
    ``Vector database`` and ``Retrieval-augmented generation`` qualify;
    ``pgvector hybrid search benchmark recall`` does not.
    """
    if not isinstance(query, str) or not query.strip():
        return False
    if re.search(r"\b[a-z]+:\S", query, flags=re.I):
        return False
    words = [w for w in re.split(r"\s+", query.strip()) if re.search(r"\w", w)]
    return 0 < len(words) <= _ENTITY_MAX_WORDS


def _active_general_engines(entity_lookup: bool = True) -> tuple:
    """Return ``(pin, all_cooling)``: the configured pin minus cooling engines.

    Title-lookup engines (``_ENTITY_ONLY_ENGINES``) stay in the pin only when
    *entity_lookup* is True (the query could be an article title, see
    ``_entity_like``). ``all_cooling`` is True when a pin is configured but
    every engine in it that searches the web is cooling down. The caller then
    asks SearXNG's own defaults instead.
    """
    from .resilience import cooldowns

    pinned = _general_engines()
    active = [
        e for e in pinned
        if not cooldowns.is_cooling(_engine_cooldown_key(e))
        and (entity_lookup or e.casefold() not in _ENTITY_ONLY_ENGINES)
    ]
    searching = [e for e in active if e.casefold() not in _ENTITY_ONLY_ENGINES]
    web_pinned = [e for e in pinned if e.casefold() not in _ENTITY_ONLY_ENGINES]
    if web_pinned:
        exhausted = not searching
    else:
        # An operator pinned only title-lookup engines: honour that as long
        # as one of them may be asked.
        exhausted = bool(pinned) and not active
    if exhausted:
        return [], bool(pinned)
    return active, False


def _note_unresponsive_engines(unresponsive) -> None:
    """Cool down pinned engines SearXNG reported as blocked/suspended."""
    from .resilience import cooldowns

    pinned = set(_general_engines())
    seconds = _engine_cooldown_seconds()
    for entry in unresponsive or []:
        if not isinstance(entry, (list, tuple)) or not entry:
            continue
        name = str(entry[0])
        reason = str(entry[1]) if len(entry) > 1 else ""
        if name not in pinned:
            continue
        if any(marker in reason.casefold() for marker in _ENGINE_BLOCK_MARKERS):
            cooldowns.cool(_engine_cooldown_key(name), seconds, f"SearXNG reported {reason!r}")


def _engine_tally(results) -> str:
    """``"bing=14, wikipedia=1"`` for the engines behind SearXNG's rows."""
    counts: dict = {}
    for row in results or []:
        if not isinstance(row, dict):
            continue
        names = row.get("engines") or ([row["engine"]] if row.get("engine") else [])
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _infobox_rows(infoboxes) -> List[dict]:
    """SearXNG ``infoboxes`` entries as result-shaped rows.

    The wikipedia engine's default ``display_type: ["infobox"]`` puts its hit
    here (``{"infobox": title, "id": url, "content": extract, "urls": [...]}``),
    so reading only ``results`` made it look as if it never answered.
    """
    rows: List[dict] = []
    for box in infoboxes or []:
        if not isinstance(box, dict):
            continue
        url = box.get("id") if isinstance(box.get("id"), str) else ""
        if not url.startswith(("http://", "https://")):
            url = ""
            for link in box.get("urls") or []:
                if isinstance(link, dict) and str(link.get("url") or "").startswith(("http://", "https://")):
                    url = link["url"]
                    break
        if not url:
            continue
        engines = box.get("engines") or ([box["engine"]] if box.get("engine") else [])
        rows.append({
            "title": box.get("infobox") or "",
            "url": url,
            "content": str(box.get("content") or "")[:500],
            "engines": list(engines),
        })
    return rows


def searxng_search_api(query: str, count: Optional[int] = None, categories: str = "general",
                       time_filter: Optional[str] = None) -> List[dict]:
    """Search using SearXNG JSON API. Returns list of {title, url, snippet, engine}."""
    count = count if count is not None else _get_result_count()
    instance = _get_search_instance()
    api_key = ""
    headers = {"User-Agent": WEB_FETCH_USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    q_lc = query.lower()
    # A `site:` query is a lookup on a known site (docs, a standard, a
    # licence page) — the news category has nothing for it and every such
    # query used to pay a wasted news round-trip before the general retry.
    # Likewise `time_filter="year"` is a "prefer recent" hint the agent puts
    # on ordinary lookups, not a news signal: only day/week/month or a news
    # word in the query switches category.
    scoped_to_site = bool(re.search(r"\bsite:\S+", q_lc))
    # The engines behind SearXNG ignore `site:` (off-domain rows) or answer
    # nothing at all (yahoo: 0 rows for `site:github.com/<owner>/<repo>`), so
    # the operator goes out as plain words: the subject, `owner/repo`, the
    # host. core._call_provider keeps the on-domain rows.
    engine_query = site_keyword_query(query) if scoped_to_site else query
    if engine_query != query:
        logger.info("SearXNG: sending %r as %r (site: operator as keywords)", query, engine_query)
    pinned_engines, pin_exhausted = _active_general_engines(
        entity_lookup=not scoped_to_site and _entity_like(engine_query)
    )
    pinned_param = ",".join(pinned_engines)
    # News/fresh queries do badly in the 'general' category — it favours
    # encyclopedic/tourism pages, ignores recency, and (with no language pin)
    # bleeds in foreign-language results. When the agent layer detected
    # freshness (time_filter) or the query reads like a news lookup, switch to
    # the 'news' category, constrain recency, and pin language to English so a
    # search like "Canada latest news" returns actual news instead of Wikipedia.
    # Pin English for ALL searches — without it, SearXNG geolocates / mixes
    # languages and brand-ambiguous terms bleed in foreign SEO pages (e.g.
    # "Odyssey" → Honda Japan, "Trojan" → Japanese malware blogs, "Polyphemus"
    # → Chinese math forums). The news path already did this; general didn't.
    params = {
        "q": engine_query,
        "format": "json",
        "language": "en",
        "safesearch": _safesearch_for("searxng"),
    }
    is_news = not scoped_to_site and (
        time_filter in ("day", "week", "month") or any(h in q_lc for h in _NEWS_HINTS)
    )

    def _pin_general(target: dict) -> None:
        # An explicit engine list replaces the category: sending both makes
        # SearXNG query the pin *and* every default engine of the category.
        if pinned_param:
            target.pop("categories", None)
            target["engines"] = pinned_param
        else:
            target["categories"] = "general"
            target.pop("engines", None)

    if is_news and categories == "general":
        params["categories"] = "news"
        if time_filter in ("day", "week", "month", "year"):
            # 'day' is too sparse on most SearXNG news engines — widen to a week
            # so there's enough volume; the news category already biases recent.
            params["time_range"] = "week" if time_filter in ("day", "week") else time_filter
    elif categories == "general":
        _pin_general(params)
    else:
        params["categories"] = categories
    if pin_exhausted and categories == "general":
        logger.info(
            "SearXNG: every pinned engine (%s) is cooling down; using SearXNG's defaults for %r",
            ",".join(_general_engines()), query,
        )
    try:
        def _parse_results(results):
            parsed = []
            seen_urls = set()
            for r in results:
                if len(parsed) >= count:
                    break
                if not isinstance(r, dict) or not r.get("url") or r["url"] in seen_urls:
                    continue
                seen_urls.add(r["url"])
                engines = r.get("engines") or ([r["engine"]] if r.get("engine") else [])
                parsed.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("content", ""),
                    # Which upstream engine(s) produced the row; logged by the
                    # relevance gate so junk can be traced to its engine.
                    "engine": ",".join(str(e) for e in engines),
                })
            return parsed

        def _rows_of(data):
            # Infobox hits first: wikipedia's (and wikidata's) article for an
            # entity query arrives in `infoboxes`, not in `results`.
            return _infobox_rows(data.get("infoboxes")) + list(data.get("results", []) or [])

        def _run(search_params):
            # Every attempt below (news -> general -> no language -> SearXNG
            # defaults) takes its timeout from the caller's search deadline;
            # once too little is left, DeadlineExceeded stops the retries.
            response = httpx.get(
                f"{instance}/search",
                params=search_params,
                headers=headers or None,
                timeout=request_timeout(15, "SearXNG"),
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                data = {}
            raw_rows = _rows_of(data)
            _note_unresponsive_engines(data.get("unresponsive_engines"))
            logger.info(
                "SearXNG answered %d row(s) for %r (engines: %s; asked: %s)",
                len(raw_rows), engine_query, _engine_tally(raw_rows) or "none",
                search_params.get("engines") or f"categories={search_params.get('categories')}",
            )
            return _parse_results(raw_rows), data

        active_params = params
        parsed, data = _run(active_params)
        if not parsed and is_news and categories == "general":
            # Some self-hosted SearXNG configs have no working news engines.
            # Fall back to the known-good general engines before reporting an
            # empty search, otherwise common queries like "Canada news" fail.
            fallback = {
                "q": engine_query,
                "format": "json",
                "language": "en",
                "safesearch": _safesearch_for("searxng"),
            }
            _pin_general(fallback)
            logger.info(
                "SearXNG news search returned 0 results for %r; retrying general engines",
                query,
            )
            active_params = fallback
            parsed, data = _run(active_params)
        if not parsed and active_params.get("language"):
            fallback = dict(active_params)
            fallback.pop("language", None)
            logger.info(
                "SearXNG language-pinned search returned 0 results for %r; retrying without language",
                query,
            )
            active_params = fallback
            parsed, data = _run(active_params)
        if not parsed and active_params.get("engines"):
            fallback = dict(active_params)
            fallback.pop("engines", None)
            fallback["categories"] = "general"
            logger.info(
                "SearXNG pinned engines returned 0 results for %r; retrying default engines",
                query,
            )
            parsed, data = _run(fallback)
        logger.info(f"SearXNG JSON API returned {len(parsed)} results for: {query}")
        # Logged whether or not rows came back: SearXNG answered 5 rows for
        # all 344 queries on 2026-09-27 while the relevance gate threw away
        # every row for half of them, and which engines were blocked
        # (CAPTCHA / 429 / timeout) is the only clue to why.
        unresponsive = data.get("unresponsive_engines") if isinstance(data, dict) else None
        if unresponsive:
            logger.info(f"SearXNG unresponsive engines for {query!r}: {unresponsive}")
        return parsed
    except DeadlineExceeded as e:
        # Out of time between attempts: hand back what the last attempt got
        # (usually nothing) instead of starting another request.
        logger.info("SearXNG: %s for %r; stopping retries", e, query)
        return []
    except Exception as e:
        logger.warning(f"SearXNG JSON API search failed: {e}")
        if out_of_time():
            return []
        html_results = searxng_search(engine_query, max_results=count)
        if html_results:
            logger.info(f"SearXNG HTML fallback returned {len(html_results)} results for: {query}")
        return html_results


def searxng_search(query, max_results=10):
    """Search using SearXNG instance - parsing HTML."""
    instance = _get_search_instance()
    api_key = ""
    req_headers = {"User-Agent": WEB_FETCH_USER_AGENT}
    if api_key:
        req_headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = httpx.get(
            f"{instance}/search",
            params={"q": query, "safesearch": _safesearch_for("searxng")},
            headers=req_headers,
            timeout=request_timeout(10, "SearXNG HTML"),
        )
        if response.is_success:
            soup = BeautifulSoup(response.text, "html.parser")
            results = []
            for article in soup.select("article.result")[:max_results]:
                title_elem = article.select_one("h3 a")
                if not title_elem:
                    continue
                title = title_elem.get_text(strip=True)
                url = title_elem.get("href", "")
                snippet_elem = article.select_one("p.content")
                snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""
                results.append({"title": title, "url": url, "snippet": snippet})
            logger.info(f"SearXNG search (HTML) returned {len(results)} results")
            return results
    except Exception as e:
        logger.error(f"SearXNG search failed: {e}")
    return []


# ── GitHub issue search (public API, optional) ──

_GITHUB_API = "https://api.github.com"
_GITHUB_SNIPPET_CHARS = 300


def github_issue_search_enabled() -> bool:
    """On unless ODYSSEUS_GITHUB_ISSUE_SEARCH is 0/false/off/no."""
    raw = (os.environ.get("ODYSSEUS_GITHUB_ISSUE_SEARCH") or "").strip().casefold()
    return raw not in ("0", "false", "off", "no", "disabled")


def _github_headers() -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": WEB_FETCH_USER_AGENT,
    }
    try:
        from src.github_credentials import github_token_from_env
        # public_only: an Enterprise token must never be sent to github.com.
        token = github_token_from_env(public_only=True)
    except Exception:
        token = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _github_rate_limit_wait(response) -> Optional[float]:
    """Seconds until GitHub's search quota resets, or None when not limited."""
    status = getattr(response, "status_code", 0)
    if status not in (403, 429):
        return None
    hdrs = getattr(response, "headers", {}) or {}
    retry_after = hdrs.get("retry-after") or hdrs.get("Retry-After")
    if retry_after:
        try:
            return min(600.0, max(5.0, float(retry_after)))
        except ValueError:
            pass
    remaining = hdrs.get("x-ratelimit-remaining") or hdrs.get("X-RateLimit-Remaining")
    reset = hdrs.get("x-ratelimit-reset") or hdrs.get("X-RateLimit-Reset")
    if status == 429 or remaining == "0":
        try:
            import time as _time
            return min(600.0, max(5.0, float(reset) - _time.time()))
        except (TypeError, ValueError):
            return 60.0
    return None


# GitHub's "best match" order weighs text matches in comments and labels as
# much as the title and ignores state and age, so the top rows for "stale
# vectors after delete" were often long-closed issues that mention both words
# somewhere in a 200-comment thread. Rows are re-ranked here (the chain keeps
# this order; see core._rank_rows): title matches count most, then body
# matches, then open over closed, recent over old and discussed over
# ignored. GitHub's own position only breaks near-ties.
_GH_WEIGHTS = {
    "title": 3.0,          # share of query terms in the title
    "title_phrase": 0.75,  # the terms appear in the title in query order
    "body": 1.0,           # share of query terms in the body
    "open": 0.5,
    "closed_completed": 0.25,  # closed as fixed often holds the answer
    "recency": 0.75,       # decays with a ~6 month half-life
    "engagement": 0.5,     # reactions + comments, log-scaled
    "position": 0.15,      # GitHub's best-match order (tie-breaker)
}
_GH_RECENCY_HALF_LIFE_DAYS = 180.0
_GH_ENGAGEMENT_SATURATION = 50  # this many reactions+comments count as "a lot"
_GH_BODY_SCAN_CHARS = 4000


def _gh_stem(term: str) -> str:
    """Drop a plural/verb ending so "delete" also finds "deleting"/"deleted"."""
    for suffix in ("ing", "ies", "es", "ed", "s", "e"):
        if len(term) - len(suffix) >= 4 and term.endswith(suffix):
            return term[: -len(suffix)]
    return term


def _gh_term_hits(stems: List[str], text: str) -> int:
    """How many *stems* start a word in *text*."""
    return sum(1 for t in stems if re.search(r"\b" + re.escape(t), text))


def _gh_age_days(stamp, now) -> Optional[float]:
    from datetime import datetime, timezone

    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (now - when).total_seconds() / 86400.0)


# Repo-less GitHub queries ("GitHub issue <subject>", site:github.com/issues
# ...) search every public repository, and GitHub ANDs the terms, so a tiny
# personal repo whose own issue tracker happens to use the same words matches
# as well as a real project. On 2026-09-28 DGAFP/assistant-rh#471,
# fpt/rs-gallium#289 and Early-Bird-Solutions-LLC/PinballWizard#588 -- tiny
# repos, no engagement -- ranked high and one of them became a headline
# finding in a research report. The search response has no star count (only
# ``repository_url``; a stars lookup would cost one more API call per
# repository against a 10-30/minute quota), so the quality signals are the
# ones each item carries plus how many hits share a repository:
#
# * engagement (reactions + comments) weighs more than on a repository search;
# * a repository with several matching issues gets a small bonus (the topic
#   recurs there), a single hit gets none;
# * an issue its repository's owner filed that nobody answered, or one a bot
#   opened, loses a little;
# * below the engagement floor (_GH_MIN_ENGAGEMENT) an issue is "low signal":
#   it is sorted after every issue that has engagement, whatever its text
#   score, and single-hit repositories come last. Its row carries
#   ``low_signal: True`` so the chain can add web results when GitHub found
#   nothing better (core._run_chain).
#
# Repository-scoped queries (``repo:owner/name``) keep the plain ranking: every
# row comes from the repository the model asked about.
_GH_MIN_ENGAGEMENT = 1
_GH_REPOLESS_WEIGHTS = {
    "engagement": 1.25,    # replaces _GH_WEIGHTS["engagement"]
    "repo_breadth": 0.3,   # several hits in one repository (saturates at 4)
    "solo_owner": -0.4,    # the owner filed it, no reactions or comments
    "bot": -0.4,           # opened by a bot account
}


def _gh_activity(item: dict) -> int:
    """Reactions plus comments of a GitHub search item (0 when malformed)."""
    reactions = item.get("reactions") if isinstance(item.get("reactions"), dict) else {}
    try:
        reacted = max(0, int(reactions.get("total_count") or 0))
    except (TypeError, ValueError):
        reacted = 0
    try:
        commented = max(0, int(item.get("comments") or 0))
    except (TypeError, ValueError):
        commented = 0
    return reacted + commented


def _gh_repo_key(item: dict) -> str:
    repo_url = str(item.get("repository_url") or "")
    if "/repos/" in repo_url:
        return repo_url.split("/repos/", 1)[1].casefold()
    match = re.match(r"https?://github\.com/([^/]+/[^/]+)/", str(item.get("html_url") or ""), flags=re.I)
    return match.group(1).casefold() if match else ""


def _github_issue_score(item: dict, terms: List[str], position: int, total: int, now,
                        repo_hits: Optional[int] = None) -> float:
    """Relevance of one GitHub search item to the query *terms* (higher first).

    *repo_hits* is given for repository-less searches only: how many items in
    the result set share this item's repository. It switches on the quality
    weighting described above ``_GH_MIN_ENGAGEMENT``.
    """
    import math

    w = _GH_WEIGHTS
    terms = [_gh_stem(t.casefold()) for t in terms if t]
    title = str(item.get("title") or "").casefold()
    body = str(item.get("body") or "")[:_GH_BODY_SCAN_CHARS].casefold()
    score = 0.0
    if terms:
        score += w["title"] * _gh_term_hits(terms, title) / len(terms)
        score += w["body"] * _gh_term_hits(terms, body) / len(terms)
        if len(terms) > 1 and re.search(
            r"\b" + r"\w*\W+(?:\w+\W+){0,2}".join(re.escape(t) for t in terms), title
        ):
            score += w["title_phrase"]
    state = str(item.get("state") or "").casefold()
    if state == "open":
        score += w["open"]
    elif state == "closed" and str(item.get("state_reason") or "").casefold() == "completed":
        score += w["closed_completed"]
    age = _gh_age_days(item.get("updated_at") or item.get("created_at"), now)
    if age is not None:
        score += w["recency"] * 0.5 ** (age / _GH_RECENCY_HALF_LIFE_DAYS)
    activity = _gh_activity(item)
    engagement_weight = w["engagement"] if repo_hits is None else _GH_REPOLESS_WEIGHTS["engagement"]
    if activity > 0:
        score += engagement_weight * min(
            1.0, math.log1p(activity) / math.log1p(_GH_ENGAGEMENT_SATURATION)
        )
    if repo_hits is not None:
        rw = _GH_REPOLESS_WEIGHTS
        score += rw["repo_breadth"] * min(1.0, max(0, repo_hits - 1) / 3.0)
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        if str(user.get("type") or "").casefold() == "bot":
            score += rw["bot"]
        elif activity == 0 and str(item.get("author_association") or "").upper() == "OWNER":
            score += rw["solo_owner"]
    if total > 1:
        score += w["position"] * (1.0 - position / (total - 1))
    elif total == 1:
        score += w["position"]
    return score


def _gh_low_signal(item: dict) -> bool:
    """Below the engagement floor: nobody reacted to or commented on it."""
    return _gh_activity(item) < _GH_MIN_ENGAGEMENT


def rank_github_issues(items, terms: List[str], now=None, repo_scoped: bool = True) -> List[dict]:
    """GitHub search *items* ordered by :func:`_github_issue_score`.

    With ``repo_scoped=False`` (a search across all of GitHub) issues below
    the engagement floor are sorted after every engaged issue, and among
    them issues from repositories with a single hit come last; see the
    comment above ``_GH_MIN_ENGAGEMENT``. Ties keep GitHub's order (the sort
    is stable).
    """
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    usable = [it for it in (items or []) if isinstance(it, dict) and it.get("html_url")]
    hits: dict = {}
    if not repo_scoped:
        for it in usable:
            key = _gh_repo_key(it)
            hits[key] = hits.get(key, 0) + 1

    def tier(it) -> int:
        if repo_scoped or not _gh_low_signal(it):
            return 0
        return 1 if hits.get(_gh_repo_key(it), 0) > 1 else 2

    scored = [
        (tier(it),
         _github_issue_score(it, terms, i, len(usable), now,
                             None if repo_scoped else hits.get(_gh_repo_key(it), 1)),
         it)
        for i, it in enumerate(usable)
    ]
    scored.sort(key=lambda entry: (entry[0], -entry[1]))
    return [it for _, _, it in scored]


def _gh_count_label(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _github_issue_rows(items, count: int, terms: Optional[List[str]] = None, now=None,
                       repo_scoped: bool = True) -> List[dict]:
    rows = []
    for item in rank_github_issues(items, list(terms or []), now=now, repo_scoped=repo_scoped):
        repo = ""
        repo_url = item.get("repository_url") or ""
        if "/repos/" in repo_url:
            repo = repo_url.split("/repos/", 1)[1]
        number = item.get("number")
        state = item.get("state") or ""
        kind = "PR" if item.get("pull_request") else "Issue"
        title = str(item.get("title") or "").strip()
        label = f"{title} · {kind} #{number}" if number is not None else title
        if repo:
            label += f" · {repo}"
        details = [str(state)] if state else []
        if not repo_scoped:
            # Across all of GitHub the model should see how much attention an
            # issue got before treating it as evidence.
            comments = item.get("comments") if isinstance(item.get("comments"), int) else 0
            reactions = item.get("reactions") if isinstance(item.get("reactions"), dict) else {}
            reacted = reactions.get("total_count") if isinstance(reactions.get("total_count"), int) else 0
            details.append(_gh_count_label(comments, "comment"))
            if reacted:
                details.append(_gh_count_label(reacted, "reaction"))
        if details:
            label += f" ({', '.join(details)})"
        body = re.sub(r"\s+", " ", str(item.get("body") or "")).strip()
        row = {
            "title": label,
            "url": item["html_url"],
            "snippet": body[:_GITHUB_SNIPPET_CHARS],
            "age": str(item.get("updated_at") or "")[:10],
            "engine": "github",
        }
        if not repo_scoped and _gh_low_signal(item):
            row["low_signal"] = True
        rows.append(row)
        if len(rows) >= count:
            break
    return rows


def github_issue_search(query: str, count: Optional[int] = None, time_filter: Optional[str] = None) -> List[dict]:
    """Answer a GitHub-issue-scoped query from GitHub's issue search API.

    Only queries :func:`services.search.query.github_issue_scope` accepts are
    sent (``site:github.com/<org>/<repo>/issues ...`` and friends). GitHub
    ANDs every term, so the query carries at most five subject terms and is
    retried once with the first three when the longer form matches nothing.
    Uses ``GITHUB_PERSONAL_ACCESS_TOKEN`` when set for github.com (30
    searches/minute instead of 10). When the quota runs out the provider
    cools down until GitHub's reset time and the chain falls through to
    SearXNG. ``time_filter`` is ignored: issue relevance does not track a
    year-wide recency hint.
    """
    from .query import github_issue_scope
    from .resilience import cooldowns

    count = count if count is not None else _get_result_count()
    if not github_issue_search_enabled():
        return []
    scope = github_issue_scope(query)
    if not scope:
        return []
    if cooldowns.is_cooling("github issue search"):
        return []
    qualifiers = [f"is:{scope['kind']}"]
    if scope.get("repo"):
        qualifiers.insert(0, f"repo:{scope['repo']}")
    terms = list(scope["terms"])
    attempts = [terms]
    if len(terms) > 3:
        attempts.append(terms[:3])
    headers = _github_headers()
    for attempt in attempts:
        q = " ".join(attempt + qualifiers)
        try:
            response = httpx.get(
                f"{_GITHUB_API}/search/issues",
                # Ask for more than *count*: the rows are re-ranked locally
                # (rank_github_issues) and GitHub's top few are often not
                # the best matches.
                params={"q": q, "per_page": max(1, min(max(int(count) * 3, 20), 50))},
                headers=headers,
                timeout=request_timeout(REQUEST_TIMEOUT, "GitHub issue search"),
            )
        except httpx.HTTPError as e:
            logger.warning("GitHub issue search failed for %r: %s", q, e)
            return []
        wait = _github_rate_limit_wait(response)
        if wait is not None:
            cooldowns.cool("github issue search", wait, f"rate limit reached (HTTP {response.status_code})")
            return []
        if response.status_code == 422:
            # Invalid query (e.g. an unknown repo): nothing to find there.
            logger.info("GitHub issue search rejected %r (HTTP 422)", q)
            return []
        try:
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("GitHub issue search failed for %r: %s", q, e)
            return []
        rows = _github_issue_rows(data.get("items") if isinstance(data, dict) else None, count, attempt,
                                  repo_scoped=bool(scope.get("repo")))
        low = sum(1 for r in rows if r.get("low_signal"))
        logger.info("GitHub issue search %r returned %d row(s)%s", q, len(rows),
                    f" ({low} with no comments or reactions)" if low else "")
        if rows:
            return rows
    return []


# ── Brave ──

def brave_search(query: str, count: Optional[int] = None, time_filter: Optional[str] = None) -> List[dict]:
    """Search using Brave API with key from admin settings or env var."""
    count = count if count is not None else _get_result_count()
    api_key = _get_provider_key("brave") or os.environ.get("DATA_BRAVE_API_KEY") or ""
    return _brave_search_impl(query, count, time_filter, search_config={"brave_api_key": api_key})


def _brave_search_impl(query: str, count: int, time_filter: Optional[str] = None, search_config: dict = None) -> List[dict]:
    """Core Brave API call. Returns a list of result dicts or an empty list on failure."""
    enhanced_query = build_enhanced_query(query, time_filter)
    config = search_config or {}

    brave_api_key = config.get("brave_api_key")
    if not brave_api_key:
        brave_api_key = os.environ.get("DATA_BRAVE_API_KEY")

    if not brave_api_key:
        logger.warning("Brave API key not found, returning empty results for fallback")
        return []

    headers = {"X-Subscription-Token": brave_api_key, "Accept": "application/json"}
    params = {
        "q": enhanced_query,
        "count": count,
        "safesearch": _safesearch_for("brave"),
    }
    if time_filter:
        time_map = {"day": "day", "week": "week", "month": "month", "year": "year"}
        if time_filter in time_map:
            params["freshness"] = time_map[time_filter]

    logger.info(f"Executing Brave search with query: {enhanced_query}")
    try:
        response = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers=headers,
            params=params,
            timeout=request_timeout(REQUEST_TIMEOUT, "Brave"),
        )
        if response.status_code == 429:
            raise RateLimitError("Brave rate limit hit")
        response.raise_for_status()
    except httpx.RequestError as e:
        error_logger.error(f"NetworkError during Brave search: {e}")
        return []
    except RateLimitError as e:
        error_logger.error(str(e))
        return []

    try:
        data = response.json()
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse Brave API response: {e}")
        return []

    results = []
    if "web" in data and "results" in data["web"]:
        for item in data["web"]["results"][:count]:
            url = item.get("url", "")
            if not url:
                continue
            results.append({
                "title": item.get("title", ""),
                "url": url,
                "snippet": item.get("description", "") or item.get("content", ""),
                "age": item.get("date", "") if item.get("date") else "",
            })

    logger.info(f"Brave search returned {len(results)} results")
    return results


# ── DuckDuckGo (free, no key) ──

def _is_duckduckgo_host(host: str) -> bool:
    """True only for duckduckgo.com and its subdomains."""
    host = (host or "").lower()
    return host == "duckduckgo.com" or host.endswith(".duckduckgo.com")


def _resolve_ddg_redirect(raw: str) -> str:
    """Resolve a DuckDuckGo /l/?uddg= redirect URL to its destination."""
    if not raw:
        return raw
    resolved = raw
    if resolved.startswith("//"):
        resolved = "https:" + resolved
    elif resolved.startswith("/"):
        resolved = urljoin("https://html.duckduckgo.com", resolved)
    try:
        parsed = urlparse(resolved)
        if _is_duckduckgo_host(parsed.hostname) and parsed.path.rstrip("/") == "/l":
            qs = parse_qs(parsed.query)
            if "uddg" in qs:
                return qs["uddg"][0]
    except Exception:
        pass
    return resolved


_TRANSPORT_ERROR_MARKERS = (
    "timed out", "timeout", "error sending request", "connection", "decodeerror",
    "reset", "ratelimit", "rate limit", "429", "eof occurred", "ssl",
)


def _is_transport_error(exc: BaseException) -> bool:
    """True for failures of the network path (not an honest "no results")."""
    if isinstance(exc, httpx.TransportError):
        return True
    msg = f"{type(exc).__name__} {exc}".lower()
    return any(marker in msg for marker in _TRANSPORT_ERROR_MARKERS)


def duckduckgo_search(query: str, count: Optional[int] = None, time_filter: Optional[str] = None) -> List[dict]:
    """Search DuckDuckGo via the maintained ``ddgs`` package.

    The package is optional; when it is absent, the HTML endpoint remains a
    best-effort fallback so selecting DuckDuckGo still has honest degraded
    behavior rather than an import-time failure.

    Transport outcomes feed the provider's circuit breaker
    (``resilience.get_breaker("duckduckgo")``) so that, under parallel load,
    workers stop waiting on it after a run of timeouts. When ``ddgs`` itself
    failed on the network, the direct html.duckduckgo.com request is skipped:
    ``ddgs`` already went to that endpoint, and on 2026-09-27 the follow-up
    timed out 41 times and returned a result zero times.
    """
    from .resilience import get_breaker

    breaker = get_breaker("duckduckgo")
    count = count if count is not None else _get_result_count()

    def _settle(results: List[dict], failure: Optional[BaseException]) -> List[dict]:
        if results:
            breaker.record_success()
        elif failure is not None and _is_transport_error(failure):
            breaker.record_failure(str(failure))
        return results

    def _html_fallback() -> List[dict]:
        if out_of_time():
            return []
        results, failure = _html_fallback_raw()
        return _settle(results, failure)

    def _html_fallback_raw():
        try:
            response = httpx.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query, "kp": _safesearch_for("duckduckgo_html")},
                headers={"User-Agent": WEB_FETCH_USER_AGENT},
                timeout=request_timeout(REQUEST_TIMEOUT, "DuckDuckGo HTML"),
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            parsed = []
            for result in soup.select(".result")[:count]:
                link = result.select_one(".result__a")
                if not link:
                    continue
                url = _resolve_ddg_redirect(link.get("href", ""))
                if not url:
                    continue
                snippet_el = result.select_one(".result__snippet")
                parsed.append({
                    "title": link.get_text(" ", strip=True),
                    "url": url,
                    "snippet": snippet_el.get_text(" ", strip=True) if snippet_el else "",
                })
            logger.info(f"DuckDuckGo HTML search returned {len(parsed)} results")
            return parsed, None
        except Exception as e:
            logger.warning(f"DuckDuckGo HTML search failed: {e}")
            return [], e

    try:
        from ddgs import DDGS
    except ImportError:
        logger.warning("ddgs package not installed; using HTML fallback")
        return _html_fallback()

    timelimit = None
    if time_filter:
        time_map = {"day": "d", "week": "w", "month": "m", "year": "y"}
        timelimit = time_map.get(time_filter)

    try:
        # ddgs has its own per-request timeout (5 s by default); keep it
        # inside the search deadline.
        ddg_timeout = max(1, int(request_timeout(REQUEST_TIMEOUT, "DuckDuckGo")))
        try:
            ddgs = DDGS(timeout=ddg_timeout)
        except TypeError:
            ddgs = DDGS()
        raw = ddgs.text(
            query,
            max_results=count,
            timelimit=timelimit,
            safesearch=_safesearch_for("duckduckgo_lib"),
        )
        results = []
        for item in raw:
            url = item.get("href", "")
            if not url:
                continue
            results.append({
                "title": item.get("title", ""),
                "url": url,
                "snippet": item.get("body", ""),
            })
        logger.info(f"DuckDuckGo search returned {len(results)} results")
    except DeadlineExceeded:
        raise
    except Exception as e:
        logger.warning(f"DuckDuckGo search failed: {e}")
        if _is_transport_error(e):
            return _settle([], e)
        return _html_fallback()
    if results:
        return _settle(results, None)
    return _html_fallback()


# ── Google Programmable Search Engine ──

def google_pse_search(query: str, count: Optional[int] = None, time_filter: Optional[str] = None) -> List[dict]:
    """Search using Google PSE (Custom Search JSON API).

    Requires two keys in settings:
      - search_api_key: Google API key
      - google_pse_cx: Programmable Search Engine ID (cx)
    Or env vars GOOGLE_API_KEY and GOOGLE_PSE_CX.
    """
    count = count if count is not None else _get_result_count()
    settings = _get_search_settings()
    api_key = _get_provider_key("google_pse") or os.environ.get("GOOGLE_API_KEY", "")
    cx = (settings.get("google_pse_cx") or "").strip() or os.environ.get("GOOGLE_PSE_CX", "")

    if not api_key or not cx:
        logger.warning("Google PSE: missing API key or CX ID")
        return []

    params = {
        "key": api_key,
        "cx": cx,
        "q": query,
        "num": min(count, 10),  # Google PSE max is 10 per request
    }
    safe = _safesearch_for("google_pse")
    if safe:
        params["safe"] = safe
    if time_filter:
        # dateRestrict: d[number], w[number], m[number], y[number]
        time_map = {"day": "d1", "week": "w1", "month": "m1", "year": "y1"}
        if time_filter in time_map:
            params["dateRestrict"] = time_map[time_filter]

    try:
        response = httpx.get(
            "https://www.googleapis.com/customsearch/v1",
            params=params,
            timeout=request_timeout(REQUEST_TIMEOUT, "Google PSE"),
        )
        if response.status_code == 429:
            raise RateLimitError("Google PSE rate limit hit")
        response.raise_for_status()
    except httpx.RequestError as e:
        error_logger.error(f"Google PSE search failed: {e}")
        return []
    except RateLimitError as e:
        error_logger.error(str(e))
        return []

    try:
        data = response.json()
    except json.JSONDecodeError as e:
        error_logger.error(f"Google PSE returned invalid JSON: {e}")
        return []

    results = []
    for item in data.get("items", [])[:count]:
        url = item.get("link", "")
        if not url:
            continue
        results.append({
            "title": item.get("title", ""),
            "url": url,
            "snippet": item.get("snippet", ""),
        })

    logger.info(f"Google PSE returned {len(results)} results")
    return results


# ── Tavily ──

def tavily_search(query: str, count: Optional[int] = None, time_filter: Optional[str] = None) -> List[dict]:
    """Search using Tavily API. Requires search_api_key or TAVILY_API_KEY env var."""
    count = count if count is not None else _get_result_count()
    api_key = _get_provider_key("tavily") or os.environ.get("TAVILY_API_KEY", "")
    if not api_key:
        logger.warning("Tavily: no API key configured")
        return []

    payload = {
        "query": query,
        "max_results": count,
        "include_answer": False,
    }
    if time_filter:
        time_map = {"day": "day", "week": "week", "month": "month", "year": "year"}
        if time_filter in time_map:
            payload["days"] = {"day": 1, "week": 7, "month": 30, "year": 365}[time_filter]

    try:
        response = httpx.post(
            "https://api.tavily.com/search",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=request_timeout(REQUEST_TIMEOUT, "Tavily"),
        )
        if response.status_code == 429:
            raise RateLimitError("Tavily rate limit hit")
        response.raise_for_status()
    except httpx.RequestError as e:
        error_logger.error(f"Tavily search failed: {e}")
        return []
    except RateLimitError as e:
        error_logger.error(str(e))
        return []

    try:
        data = response.json()
    except json.JSONDecodeError as e:
        error_logger.error(f"Tavily returned invalid JSON: {e}")
        return []

    results = []
    for item in data.get("results", [])[:count]:
        url = item.get("url", "")
        if not url:
            continue
        results.append({
            "title": item.get("title", ""),
            "url": url,
            "snippet": item.get("content", ""),
            "age": item.get("published_date", ""),
        })

    logger.info(f"Tavily returned {len(results)} results")
    return results


# ── Serper.dev ──

def serper_search(query: str, count: Optional[int] = None, time_filter: Optional[str] = None) -> List[dict]:
    """Search using Serper.dev API. Requires search_api_key or SERPER_API_KEY env var."""
    count = count if count is not None else _get_result_count()
    api_key = _get_provider_key("serper") or os.environ.get("SERPER_API_KEY", "")
    if not api_key:
        logger.warning("Serper: no API key configured")
        return []

    payload = {
        "q": query,
        "num": count,
    }
    safe = _safesearch_for("serper")
    if safe:
        payload["safe"] = safe
    if time_filter:
        time_map = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}
        if time_filter in time_map:
            payload["tbs"] = time_map[time_filter]

    try:
        response = httpx.post(
            "https://google.serper.dev/search",
            json=payload,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            timeout=request_timeout(REQUEST_TIMEOUT, "Serper"),
        )
        if response.status_code == 429:
            raise RateLimitError("Serper rate limit hit")
        response.raise_for_status()
    except httpx.RequestError as e:
        error_logger.error(f"Serper search failed: {e}")
        return []
    except RateLimitError as e:
        error_logger.error(str(e))
        return []

    try:
        data = response.json()
    except json.JSONDecodeError as e:
        error_logger.error(f"Serper returned invalid JSON: {e}")
        return []

    results = []
    for item in data.get("organic", [])[:count]:
        url = item.get("link", "")
        if not url:
            continue
        results.append({
            "title": item.get("title", ""),
            "url": url,
            "snippet": item.get("snippet", ""),
            "age": item.get("date", ""),
        })

    logger.info(f"Serper returned {len(results)} results")
    return results
