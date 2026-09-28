"""One deadline for a whole search call (the agent's 30 s web_search limit).

Before the deadline, each layer had its own timeout: SearXNG could be asked
four times at 15 s each plus a 10 s HTML fallback, a `site:` miss asked
again, an empty chain re-ran with a simplified query, DuckDuckGo came next,
and page fetches ran last. The tool's ``asyncio.wait_for(..., 30)`` then threw
the whole answer away. These tests drive the layers with fake slow providers
(simulated time through ``resilience._now``, or real blocking where the
backstop itself is under test) and check that the call stops at the deadline
and returns what it has.
"""

import asyncio
import sys
import threading
import time
import types

import pytest

from services.search import core, providers, resilience


def _row(url, title="", snippet=""):
    return {"url": url, "title": title, "snippet": snippet}


class _Clock:
    """Simulated time: slow fakes advance ``t`` instead of sleeping."""

    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(round(seconds, 6))
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(resilience, "_now", c.now)
    monkeypatch.setattr(resilience, "_sleep", c.sleep)
    return c


@pytest.fixture
def searxng_then_ddg(monkeypatch):
    monkeypatch.setattr(
        core, "_get_search_settings",
        lambda: {"search_provider": "searxng", "search_fallback_chain": ["duckduckgo"]},
    )
    monkeypatch.setattr(core, "_get_result_count", lambda: 3)
    monkeypatch.delenv("ODYSSEUS_GITHUB_ISSUE_SEARCH", raising=False)
    monkeypatch.delenv(resilience.SEARCH_DEADLINE_ENV, raising=False)


# ----------------------------------------------------------------------
# Deadline primitives
# ----------------------------------------------------------------------
def test_default_deadline_is_thirty_seconds_and_configurable(monkeypatch):
    monkeypatch.delenv(resilience.SEARCH_DEADLINE_ENV, raising=False)
    assert resilience.search_deadline_seconds() == 30.0
    monkeypatch.setenv(resilience.SEARCH_DEADLINE_ENV, "12")
    assert resilience.search_deadline_seconds() == 12.0
    monkeypatch.setenv(resilience.SEARCH_DEADLINE_ENV, "nonsense")
    assert resilience.search_deadline_seconds() == 30.0


def test_request_timeouts_are_cut_to_what_is_left(clock):
    assert resilience.request_timeout(15) == 15, "no deadline: the cap applies"
    with resilience.deadline_scope(10):
        assert resilience.request_timeout(15) == 10
        clock.t += 7
        assert resilience.request_timeout(15) == pytest.approx(3)
        clock.t += 2.5
        with pytest.raises(resilience.DeadlineExceeded):
            resilience.request_timeout(15)
    assert resilience.current_deadline() is None


def test_a_nested_scope_never_extends_the_outer_deadline(clock):
    with resilience.deadline_scope(5) as outer:
        with resilience.deadline_scope(60) as inner:
            assert inner is outer
        with resilience.deadline_scope(2) as tighter:
            assert tighter.remaining() == 2


def test_backstop_returns_at_the_deadline_while_the_worker_is_stuck():
    release = threading.Event()
    deadline = resilience.SearchDeadline(0.2)
    started = time.monotonic()
    try:
        out = resilience.run_within_deadline(lambda: release.wait(10) or ["late"], deadline, ["partial"])
    finally:
        release.set()
    assert out == ["partial"]
    assert time.monotonic() - started < 0.2 + resilience.DEADLINE_JOIN_GRACE + 0.5


def test_backstop_passes_results_and_errors_through():
    deadline = resilience.SearchDeadline(5)
    assert resilience.run_within_deadline(lambda: [1], deadline, []) == [1]
    with pytest.raises(ValueError):
        resilience.run_within_deadline(lambda: (_ for _ in ()).throw(ValueError("x")), deadline, [])


def test_gate_does_not_pace_a_start_past_the_deadline(clock):
    gate = resilience.ProviderGate("x", resilience.GateLimits(4, 10.0, 0.0, 30.0))
    with resilience.deadline_scope(5):
        with gate.slot():
            pass
        with pytest.raises(resilience.ProviderBusy):
            with gate.slot():
                pytest.fail("the paced start is 10 s away, the deadline 5 s")
    assert clock.slept == []


def test_gate_slot_wait_is_bounded_by_the_deadline():
    gate = resilience.ProviderGate("x", resilience.GateLimits(1, 0.0, 0.0, 30.0))
    hold = threading.Event()
    held = threading.Event()

    def holder():
        with gate.slot():
            held.set()
            hold.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(2)
    started = time.monotonic()
    try:
        with resilience.deadline_scope(1.2):
            with pytest.raises(resilience.ProviderBusy):
                with gate.slot():
                    pass
    finally:
        hold.set()
        t.join(5)
    assert time.monotonic() - started < 2.5, "waited for the gate's 30 s, not the deadline"


def test_single_flight_waiter_gives_up_at_its_deadline():
    cache = resilience.SingleFlightCache()
    release = threading.Event()
    computing = threading.Event()

    def slow():
        computing.set()
        release.wait(5)
        return "value"

    t = threading.Thread(target=lambda: cache.get_or_compute("k", slow, lambda v: 60))
    t.start()
    computing.wait(2)
    started = time.monotonic()
    try:
        with resilience.deadline_scope(1.1):
            with pytest.raises(resilience.DeadlineExceeded):
                cache.get_or_compute("k", lambda: pytest.fail("must wait, not compute"), lambda v: 60)
    finally:
        release.set()
        t.join(5)
    assert time.monotonic() - started < 2.5


# ----------------------------------------------------------------------
# SearXNG's own retries
# ----------------------------------------------------------------------
def _slow_empty_searxng(monkeypatch, clock, seconds_per_request):
    timeouts = []
    html_calls = []

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    def fake_get(url, **kwargs):
        timeout = kwargs["timeout"]
        timeouts.append(round(timeout, 3))
        clock.t += min(seconds_per_request, timeout)
        return _Response()

    monkeypatch.setattr(providers, "_get_search_instance", lambda: "http://searx.test")
    monkeypatch.setattr(providers, "_get_search_settings", lambda: {})
    monkeypatch.setattr(providers.httpx, "get", fake_get)
    monkeypatch.setattr(providers, "searxng_search", lambda *a, **k: html_calls.append(1) or [])
    return timeouts, html_calls


def test_searxng_retries_share_the_deadline_and_stop_when_it_runs_out(monkeypatch, clock):
    monkeypatch.delenv("SEARXNG_GENERAL_ENGINES", raising=False)
    timeouts, html_calls = _slow_empty_searxng(monkeypatch, clock, seconds_per_request=12)
    with resilience.deadline_scope(30) as deadline:
        rows = providers.searxng_search_api("pgvector hybrid search", count=5)
        assert deadline.expired()
    assert rows == []
    # pinned -> no language -> (6 s left) SearXNG defaults -> stop.
    assert timeouts == [15, 15, 6]
    assert sum(min(12, t) for t in timeouts) <= 30
    assert html_calls == [], "no HTML fallback once the deadline is spent"


def test_searxng_without_a_deadline_keeps_its_full_retry_ladder(monkeypatch, clock):
    monkeypatch.delenv("SEARXNG_GENERAL_ENGINES", raising=False)
    timeouts, _ = _slow_empty_searxng(monkeypatch, clock, seconds_per_request=12)
    providers.searxng_search_api("pgvector hybrid search", count=5)
    assert timeouts == [15, 15, 15]


# ----------------------------------------------------------------------
# Provider chain, simplified re-query, `site:` retry
# ----------------------------------------------------------------------
def test_slow_primary_leaves_no_time_for_fallbacks(monkeypatch, clock, searxng_then_ddg):
    calls = []

    def call_provider(provider, query, count, time_filter=None):
        calls.append((provider, query))
        clock.t += 25  # SearXNG ran through its whole retry ladder
        return []

    monkeypatch.setattr(core, "_call_provider", call_provider)
    msg = core.comprehensive_web_search(
        "vector store stale deleted documents chunks official documentation 2026", max_pages=2,
    )
    assert [p for p, _ in calls] == ["searxng"], "DuckDuckGo and the simplified pass are skipped"
    assert "time limit" in msg and "searxng:out of time" not in msg
    assert "duckduckgo:out of time" in msg


def test_simplified_requery_is_skipped_when_out_of_time(monkeypatch, clock, searxng_then_ddg):
    calls = []

    def call_provider(provider, query, count, time_filter=None):
        calls.append((provider, query))
        clock.t += 11
        return []

    monkeypatch.setattr(core, "_call_provider", call_provider)
    msg = core.comprehensive_web_search(
        "vector store stale deleted documents chunks official documentation 2026", max_pages=2,
    )
    # 22 s for providers (8 s kept for page fetches): searxng 11 s and
    # duckduckgo 11 s use it up, so the simplified pass never starts.
    assert len(calls) == 2
    assert {q for _, q in calls} == {
        "vector store stale deleted documents chunks official documentation 2026"
    }
    assert "stopped at its 30s time limit" in msg and "[simplified]:out of time" in msg


def test_site_retry_is_skipped_when_out_of_time(monkeypatch, clock):
    calls = []

    def raw(provider, query, count, time_filter=None):
        calls.append(query)
        clock.t += 9.5
        return [_row("https://elsewhere.test/a", "Missouri licence")]

    monkeypatch.setattr(core, "_call_provider_raw", raw)
    with resilience.deadline_scope(10):
        assert core._call_provider("searxng", "site:lucide.dev license icons", 5) == []
    assert calls == ["site:lucide.dev license icons"]


def test_results_found_before_the_deadline_are_returned(monkeypatch, clock, searxng_then_ddg):
    def call_provider(provider, query, count, time_filter=None):
        clock.t += 20
        return [_row("https://pgvector.test/hybrid", "pgvector hybrid search", "pgvector hybrid search guide")]

    monkeypatch.setattr(core, "_call_provider", call_provider)
    monkeypatch.setattr(
        core, "fetch_webpage_content",
        lambda url, *a, **k: {"success": True, "url": url, "title": "t", "content": "body"},
    )
    _, sources = core.comprehensive_web_search("pgvector hybrid search", max_pages=1, return_sources=True)
    assert [s["url"] for s in sources] == ["https://pgvector.test/hybrid"]


def test_searxng_search_results_runs_under_the_deadline(monkeypatch, clock, searxng_then_ddg, tmp_path):
    seen = []

    def call_provider(provider, query, count, time_filter=None):
        seen.append(resilience.current_deadline())
        return []

    monkeypatch.setattr(core, "_call_provider", call_provider)
    monkeypatch.setattr(core, "SEARCH_CACHE_DIR", tmp_path)
    core.searxng_search_results("pgvector hybrid search", deadline_seconds=12)
    assert seen and all(d is not None and d.seconds == 12 for d in seen)


# ----------------------------------------------------------------------
# Page fetches: partial results
# ----------------------------------------------------------------------
def test_page_fetches_stop_at_the_deadline_and_keep_what_arrived(monkeypatch, searxng_then_ddg):
    rows = [
        _row(f"https://pgvector.test/{i}", f"pgvector hybrid search {i}", "pgvector hybrid search notes")
        for i in range(3)
    ]
    monkeypatch.setattr(core, "_call_provider", lambda *a, **k: list(rows))
    release = threading.Event()
    timeouts = []

    def fetch(url, timeout, **kwargs):
        timeouts.append(timeout)
        if not url.endswith("/0"):
            release.wait(10)  # a page that hangs well past the deadline
        return {"success": True, "url": url, "title": "t", "content": f"content of {url}"}

    monkeypatch.setattr(core, "fetch_webpage_content", fetch)
    started = time.monotonic()
    try:
        text = core.comprehensive_web_search("pgvector hybrid search", max_pages=3, deadline_seconds=3)
    finally:
        release.set()
    elapsed = time.monotonic() - started
    assert elapsed < 4.5, f"search ran {elapsed:.1f}s against a 3 s deadline"
    assert "content of https://pgvector.test/0" in text
    assert "2 page(s) were not fetched" in text
    assert all(t <= 3 for t in timeouts)


# ----------------------------------------------------------------------
# Callers: agent tool, deep research
# ----------------------------------------------------------------------
def test_web_search_tool_gives_the_search_a_deadline_inside_its_limit(monkeypatch):
    from src.agent_tools.web_tools import WebSearchTool
    import src.search as search_pkg

    monkeypatch.delenv(resilience.SEARCH_DEADLINE_ENV, raising=False)
    seen = {}

    def fake_search(query, **kwargs):
        seen.update(kwargs)
        return "results", []

    monkeypatch.setattr(search_pkg, "comprehensive_web_search", fake_search)
    out = asyncio.run(WebSearchTool().execute("pgvector hybrid search", {}))
    assert out["exit_code"] == 0
    assert 0 < seen["deadline_seconds"] < 30


def _researcher():
    from src.deep_research import DeepResearcher

    r = DeepResearcher.__new__(DeepResearcher)
    r.search_provider_override = None
    r.providers_used = []
    return r


def test_deep_research_search_stops_walking_the_chain_at_the_deadline(monkeypatch, clock):
    calls = []

    def call_provider(prov, query, n):
        calls.append((prov, resilience.current_deadline()))
        clock.t += 29.5
        return []

    providers_mod = types.ModuleType("src.search.providers")
    providers_mod._get_search_settings = lambda: {"search_provider": "searxng"}
    core_mod = types.ModuleType("src.search.core")
    core_mod._build_provider_chain = lambda provider: ["searxng", "duckduckgo", "brave"]
    core_mod._call_provider = call_provider
    monkeypatch.setitem(sys.modules, "src.search.providers", providers_mod)
    monkeypatch.setitem(sys.modules, "src.search.core", core_mod)
    monkeypatch.delenv(resilience.SEARCH_DEADLINE_ENV, raising=False)

    r = _researcher()
    assert asyncio.run(r._search("anything")) == []
    assert [p for p, _ in calls] == ["searxng"]
    assert calls[0][1] is not None, "the provider thread sees the deadline"
    assert "time limit" in r._last_search_error
