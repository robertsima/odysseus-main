"""Search/fetch behaviour under parallel research load (2026-09-27 diagnostics).

~15 research workers searching at once produced 45 "No search results found"
answers: SearXNG's engines returned off-topic rows for 9-16 word queries (the
relevance gate discarded all five rows for 35 of 70 queries), DuckDuckGo
timed out 76 times with every failure followed by a 20 s HTML retry that never
returned a row, and the same 403 page was fetched seven times in three
seconds. These tests cover the guards added for that: simplified-query retry,
per-provider pacing, a DuckDuckGo circuit breaker, a single-flight result
cache, a negative fetch cache, and PDF extraction without pdfminer.six.
"""

import logging
import sys
import threading
import time
import types

import httpx
import pytest

from services.search import content as content_mod
from services.search import core, providers, resilience
from services.search.query import simplify_query


def _row(url, title="", snippet=""):
    return {"url": url, "title": title, "snippet": snippet}


class _Clock:
    """Deterministic stand-in for resilience._now/_sleep."""

    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(round(seconds, 6))


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(resilience, "_now", c.now)
    monkeypatch.setattr(resilience, "_sleep", c.sleep)
    return c


@pytest.fixture
def searxng_then_ddg(monkeypatch):
    monkeypatch.setattr(
        core,
        "_get_search_settings",
        lambda: {"search_provider": "searxng", "search_fallback_chain": ["duckduckgo"]},
    )
    monkeypatch.setattr(core, "_get_result_count", lambda: 3)
    monkeypatch.setattr(
        core,
        "fetch_webpage_content",
        lambda url, *a, **k: {"success": True, "url": url, "title": "t", "content": "body"},
    )


# ----------------------------------------------------------------------
# Query simplification
# ----------------------------------------------------------------------
def test_simplify_query_keeps_leading_subject_terms():
    q = ("2025 agent infrastructure reliability memory state tool call idempotency "
         "SDK products gaps developer tools")
    assert simplify_query(q) == "agent infrastructure reliability memory state tool"


def test_simplify_query_drops_operators_quotes_years_and_scaffolding():
    q = 'Kompose official documentation "unsupported key" OR healthcheck -spam intitle:x 2026 GitHub issues'
    assert simplify_query(q) == "Kompose unsupported key healthcheck"
    assert simplify_query('infiniflow ragflow "Automatic Folder Sync / Watcher"') == (
        "infiniflow ragflow Automatic Folder Sync Watcher"
    )


def test_simplify_query_keeps_site_scope():
    q = "site:docs.stripe.com terminal offline collect payments risks limitations documentation"
    assert simplify_query(q) == "site:docs.stripe.com terminal offline collect payments risks limitations"


def test_simplify_query_leaves_short_queries_alone():
    assert simplify_query("distinct subject") == "distinct subject"
    assert simplify_query("the of and") == "the of and"


# ----------------------------------------------------------------------
# Empty -> simplified retry
# ----------------------------------------------------------------------
def test_empty_chain_retries_once_with_simplified_query(monkeypatch, searxng_then_ddg):
    calls = []
    long_query = "offline sync conflict resolution field service utilities mapping 2025 case study"
    simplified = simplify_query(long_query)
    assert simplified != long_query

    def call_provider(provider, query, count, time_filter=None):
        calls.append((provider, query))
        if query == simplified and provider == "searxng":
            return [_row("https://docs.example/offline-sync", "Offline sync conflict resolution",
                         "Field service mapping offline")]
        return []

    monkeypatch.setattr(core, "_call_provider", call_provider)
    context, sources = core.comprehensive_web_search(long_query, return_sources=True)

    assert calls == [
        ("searxng", long_query),
        ("duckduckgo", long_query),
        ("searxng", simplified),
    ]
    assert [s["url"] for s in sources] == ["https://docs.example/offline-sync"]


def test_simplified_retry_failure_reports_both_passes(monkeypatch, searxng_then_ddg):
    monkeypatch.setattr(core, "_call_provider", lambda *a, **k: [])
    msg = core.comprehensive_web_search(
        "offline sync conflict resolution field service utilities mapping", max_pages=2
    )
    assert "searxng:empty" in msg and "searxng[simplified]:empty" in msg


def test_long_query_one_word_matches_are_not_relevant():
    query = "offline data synchronization field service utilities mapping conflict resolution"
    rows = [
        _row("https://www.merriam-webster.com/dictionary/offline",
             "Offline Definition & Meaning", "The meaning of OFFLINE is not connected"),
        _row("https://docs.example/offline-sync",
             "Offline data synchronization", "Conflict resolution for field apps"),
    ]
    kept = core._keep_relevant_results(query, rows)
    assert [r["url"] for r in kept] == ["https://docs.example/offline-sync"]
    # Short queries keep the one-token rule.
    assert core._keep_relevant_results("offline", rows[:1]) == rows[:1]


# ----------------------------------------------------------------------
# Result cache (short TTL, single flight)
# ----------------------------------------------------------------------
def test_identical_query_is_served_from_result_cache(monkeypatch, searxng_then_ddg, clock):
    calls = []

    def call_provider(provider, query, count, time_filter=None):
        calls.append(provider)
        return [_row("https://docs.example/poller", "Polling worker", "A poller")]

    monkeypatch.setattr(core, "_call_provider", call_provider)
    first = core.comprehensive_web_search("poller", return_sources=True)[1]
    second = core.comprehensive_web_search("poller", return_sources=True)[1]
    assert first == second
    assert calls == ["searxng"]

    clock.t += resilience.RESULT_TTL_HIT + 1
    core.comprehensive_web_search("poller", return_sources=True)
    assert calls == ["searxng", "searxng"]


def test_error_outcomes_are_not_cached(monkeypatch, searxng_then_ddg):
    calls = []

    def call_provider(provider, query, count, time_filter=None):
        calls.append(provider)
        raise RuntimeError("boom")

    monkeypatch.setattr(core, "_call_provider", call_provider)
    core.comprehensive_web_search("poller", max_pages=1)
    core.comprehensive_web_search("poller", max_pages=1)
    assert calls == ["searxng", "duckduckgo", "searxng", "duckduckgo"]


def test_parallel_identical_queries_share_one_provider_call(monkeypatch, searxng_then_ddg):
    calls = []
    gate = threading.Event()

    def call_provider(provider, query, count, time_filter=None):
        calls.append(provider)
        gate.wait(2)
        return [_row("https://docs.example/poller", "Polling worker", "A poller")]

    monkeypatch.setattr(core, "_call_provider", call_provider)
    out = []
    threads = [
        threading.Thread(target=lambda: out.append(core.comprehensive_web_search("poller", return_sources=True)[1]))
        for _ in range(5)
    ]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(5)

    assert calls == ["searxng"]
    assert len(out) == 5 and all(o == out[0] for o in out)


# ----------------------------------------------------------------------
# Pacing / concurrency gate
# ----------------------------------------------------------------------
def test_gate_spaces_request_starts(clock):
    gate = resilience.ProviderGate("x", resilience.GateLimits(4, 1.0, 0.0, 5.0))
    for _ in range(3):
        with gate.slot():
            pass
    assert clock.slept == [1.0, 2.0]


def test_gate_caps_concurrency_and_times_out():
    gate = resilience.ProviderGate("x", resilience.GateLimits(2, 0.0, 0.0, 0.05))
    active = 0
    peak = 0
    lock = threading.Lock()
    release = threading.Event()
    busy = []

    def worker():
        nonlocal active, peak
        try:
            with gate.slot():
                with lock:
                    active += 1
                    peak = max(peak, active)
                release.wait(1)
                with lock:
                    active -= 1
        except resilience.ProviderBusy:
            busy.append(1)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    release.set()
    for t in threads:
        t.join(3)
    assert peak == 2
    assert len(busy) == 3


def test_provider_calls_go_through_the_gate(monkeypatch):
    monkeypatch.setitem(resilience._DEFAULT_LIMITS, "searxng", resilience.GateLimits(1, 0.0, 0.0, 5.0))
    active = 0
    peak = 0
    lock = threading.Lock()

    def slow_searxng(query, count, time_filter=None):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return []

    monkeypatch.setattr(core, "searxng_search_api", slow_searxng)
    threads = [threading.Thread(target=core._call_provider_raw, args=("searxng", f"q{i}", 3))
               for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert peak == 1


def test_busy_provider_is_reported_not_raised(monkeypatch, searxng_then_ddg):
    def busy(*a, **k):
        raise resilience.ProviderBusy("searxng: no free search slot")

    monkeypatch.setattr(core, "_call_provider", busy)
    msg = core.comprehensive_web_search("poller", max_pages=1)
    assert "searxng:busy" in msg


# ----------------------------------------------------------------------
# Circuit breaker
# ----------------------------------------------------------------------
def test_breaker_trips_after_threshold_and_recovers(clock, caplog):
    caplog.set_level(logging.INFO, logger="services.search.resilience")
    b = resilience.CircuitBreaker("DuckDuckGo", threshold=3, cooldown=90)
    for _ in range(2):
        b.record_failure("timed out")
    assert b.allow()
    b.record_failure("timed out")
    assert not b.allow()
    b.record_failure("timed out")  # a straggler finishing late does not re-log
    trips = [r for r in caplog.records if "skipping it" in r.getMessage()]
    assert len(trips) == 1

    clock.t += 91
    assert b.allow(), "half-open probe after cooldown"
    assert not b.allow(), "only one probe at a time"
    b.record_success()
    assert b.allow()
    recovered = [r for r in caplog.records if "recovered" in r.getMessage()]
    assert len(recovered) == 1


def test_breaker_probe_failure_reopens(clock):
    b = resilience.CircuitBreaker("ddg", threshold=1, cooldown=10)
    b.record_failure("timed out")
    clock.t += 11
    assert b.allow()
    b.record_failure("timed out")
    assert not b.allow()
    clock.t += 11
    assert b.allow()


def _fake_ddgs(monkeypatch, behaviour):
    class FakeDDGS:
        def text(self, query, **kwargs):
            return behaviour(query)

    monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=FakeDDGS))
    monkeypatch.setattr(providers, "_get_search_settings", lambda: {})


def test_ddg_timeouts_trip_breaker_without_html_retry(monkeypatch):
    def timeout(_q):
        raise RuntimeError("error sending request for url (https://html.duckduckgo.com/html/) > operation timed out")

    _fake_ddgs(monkeypatch, timeout)
    html_calls = []
    monkeypatch.setattr(providers.httpx, "get", lambda *a, **k: html_calls.append(a) or None)

    for _ in range(3):
        assert providers.duckduckgo_search("q", count=3) == []
    assert html_calls == [], "a ddgs transport failure must not wait on html.duckduckgo.com too"
    assert not resilience.get_breaker("duckduckgo").allow()


def test_ddg_no_results_is_neutral_and_keeps_html_fallback(monkeypatch):
    def none_found(_q):
        raise RuntimeError("No results found.")

    _fake_ddgs(monkeypatch, none_found)

    class _Resp:
        text = "<html><body></body></html>"

        def raise_for_status(self):
            return None

    html_calls = []
    monkeypatch.setattr(providers.httpx, "get", lambda *a, **k: html_calls.append(a) or _Resp())
    for _ in range(4):
        providers.duckduckgo_search("q", count=3)
    assert len(html_calls) == 4
    assert resilience.get_breaker("duckduckgo").allow()


def test_open_breaker_skips_provider_in_chain(monkeypatch, searxng_then_ddg, caplog):
    for _ in range(3):
        resilience.get_breaker("duckduckgo").record_failure("timed out")
    calls = []

    def call_provider(provider, query, count, time_filter=None):
        calls.append(provider)
        return []

    monkeypatch.setattr(core, "_call_provider", call_provider)
    msg = core.comprehensive_web_search("poller", max_pages=1)
    assert calls == ["searxng"]
    assert "duckduckgo:cooling down" in msg


def test_ddg_success_closes_breaker(monkeypatch, clock):
    b = resilience.get_breaker("duckduckgo")
    for _ in range(3):
        b.record_failure("timed out")
    clock.t += b.cooldown + 1
    _fake_ddgs(monkeypatch, lambda q: [{"href": "https://a.example/", "title": "A", "body": "b"}])
    assert b.allow()  # the chain takes the half-open probe slot
    assert providers.duckduckgo_search("q", count=3)
    assert not b.is_open() and b.allow()


# ----------------------------------------------------------------------
# Negative fetch cache
# ----------------------------------------------------------------------
@pytest.fixture
def no_content_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(content_mod, "CONTENT_CACHE_DIR", tmp_path)
    monkeypatch.setattr(content_mod, "_cache_result", lambda *a, **k: None)


def _status_fetch(monkeypatch, status, calls):
    def fake(url, headers=None, timeout=5, **kwargs):
        calls.append(url)
        return httpx.Response(status, request=httpx.Request("GET", url))

    monkeypatch.setattr(content_mod, "_get_public_url", fake)


@pytest.mark.parametrize("status", [403, 404, 410, 451])
def test_permanent_fetch_failures_are_remembered(monkeypatch, no_content_cache, status, caplog):
    caplog.set_level(logging.INFO, logger="services.search.resilience")
    calls = []
    _status_fetch(monkeypatch, status, calls)
    url = f"https://www.example.com/dictionary/offline-{status}"
    first = content_mod.fetch_webpage_content(url)
    results = [content_mod.fetch_webpage_content(url + "#frag") for _ in range(6)]

    assert calls == [url]
    assert first["error"].startswith(f"HTTP {status}")
    for r in results:
        assert r["success"] is False and r["cached_failure"] is True
        assert r["error"].startswith(f"HTTP {status}"), "keeps the web_fetch hint working"
    remembered = [r for r in caplog.records if "not retrying it" in r.getMessage()]
    assert len(remembered) == 1


def test_transient_status_is_not_remembered(monkeypatch, no_content_cache):
    calls = []
    _status_fetch(monkeypatch, 500, calls)
    url = "https://www.example.com/flaky"
    content_mod.fetch_webpage_content(url)
    content_mod.fetch_webpage_content(url)
    assert calls == [url, url]


def test_negative_cache_expires(monkeypatch, no_content_cache, clock):
    calls = []
    _status_fetch(monkeypatch, 403, calls)
    url = "https://www.example.com/gone-later"
    content_mod.fetch_webpage_content(url)
    content_mod.fetch_webpage_content(url)
    clock.t += resilience.NEGATIVE_FETCH_TTL + 1
    content_mod.fetch_webpage_content(url)
    assert calls == [url, url]


def test_repeated_timeouts_are_remembered(monkeypatch, no_content_cache):
    calls = []

    def fake(url, headers=None, timeout=5, **kwargs):
        calls.append(url)
        raise httpx.ReadTimeout("timed out", request=httpx.Request("GET", url))

    monkeypatch.setattr(content_mod, "_get_public_url", fake)
    url = "https://slow.example.com/page"
    for _ in range(4):
        r = content_mod.fetch_webpage_content(url)
        assert r["success"] is False
    assert len(calls) == resilience.TIMEOUTS_BEFORE_CACHING
    assert r["cached_failure"] is True


# ----------------------------------------------------------------------
# PDF extraction without pdfminer.six
# ----------------------------------------------------------------------
def _pdf_fetch(monkeypatch):
    def fake(url, headers=None, timeout=5, **kwargs):
        return httpx.Response(
            200,
            headers={"Content-Type": "application/pdf"},
            content=b"%PDF-1.4 fake",
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(content_mod, "_get_public_url", fake)


def test_pdf_falls_back_to_pypdf_when_pdfminer_missing(monkeypatch, no_content_cache):
    _pdf_fetch(monkeypatch)
    monkeypatch.setattr(content_mod, "pdf_extract_text", None)
    monkeypatch.setattr(content_mod, "_pypdf_extract_text", lambda stream: "extracted by pypdf")
    r = content_mod.fetch_webpage_content("https://papers.example.com/a.pdf")
    assert r["success"] is True
    assert r["content"] == "extracted by pypdf"


def test_pypdf_extracts_a_real_pdf():
    pypdf = pytest.importorskip("pypdf")
    import io

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    # A blank page has no text; the point is that the backend parses real bytes.
    assert content_mod._pypdf_extract_text(io.BytesIO(buf.getvalue())) == ""


def test_missing_pdf_backends_warn_once(monkeypatch, no_content_cache, caplog):
    caplog.set_level(logging.WARNING, logger="services.search.content")
    _pdf_fetch(monkeypatch)
    monkeypatch.setattr(content_mod, "pdf_extract_text", None)
    monkeypatch.setitem(sys.modules, "pypdf", None)
    monkeypatch.setattr(content_mod, "_pdf_backend_warned", False)
    for i in range(3):
        r = content_mod.fetch_webpage_content(f"https://papers.example.com/{i}.pdf")
        assert r["success"] is False
    warnings = [r for r in caplog.records if "No PDF text extractor" in r.getMessage()]
    assert len(warnings) == 1
    assert all(r.levelno == logging.WARNING for r in warnings)
