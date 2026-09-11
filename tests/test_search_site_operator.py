"""`site:` scoping is enforced by us, not trusted to the engine.

The 2026-09-11 logs: `site:lucide.dev license accessibility icons SVG current`
returned Missouri driver-licence pages and `site:heroicons.com ...` returned
free-games portals; five off-domain pages were fetched and handed to the
model each time. A `site:` query also went to the news category first (0
results) on every call because a year-wide time filter counted as news.
"""
from services.search import core, providers


def _r(url):
    return {"title": url, "url": url, "snippet": ""}


def test_site_scope_parses_domain_forms():
    assert core._site_scope("site:lucide.dev license icons") == ("license icons", "lucide.dev")
    assert core._site_scope("ISO 29148 site:https://www.iso.org/standards/") == ("ISO 29148", "www.iso.org")
    assert core._site_scope("site:*.iso.org standard") == ("standard", "iso.org")
    assert core._site_scope("plain query") == ("plain query", None)


def test_url_on_site_matches_host_and_subdomains():
    assert core._url_on_site("https://lucide.dev/license", "lucide.dev")
    assert core._url_on_site("https://docs.lucide.dev/x", "lucide.dev")
    assert not core._url_on_site("https://notlucide.dev/x", "lucide.dev")
    assert not core._url_on_site("https://dor.mo.gov/driver-license/", "lucide.dev")


def test_off_domain_results_are_dropped_and_domain_retried_as_keyword(monkeypatch):
    calls = []

    def fake_searxng(query, count, time_filter=None):
        calls.append(query)
        if query.startswith("site:"):
            return [_r("https://dor.mo.gov/driver-license/"), _r("https://www.dmv.org/mo-missouri/renew-license.php")]
        return [_r("https://lucide.dev/license"), _r("https://example.com/unrelated")]

    monkeypatch.setattr(core, "searxng_search_api", fake_searxng)
    out = core._call_provider("searxng", "site:lucide.dev license icons", 5, "year")

    assert calls == ["site:lucide.dev license icons", "license icons lucide.dev"]
    assert [r["url"] for r in out] == ["https://lucide.dev/license"]


def test_provider_that_honours_site_is_only_filtered(monkeypatch):
    calls = []

    def fake_ddg(query, count, time_filter=None):
        calls.append(query)
        return [_r("https://www.iso.org/standard/72089.html"), _r("https://en.wikipedia.org/wiki/ISO")]

    monkeypatch.setattr(core, "duckduckgo_search", fake_ddg)
    out = core._call_provider("duckduckgo", "site:iso.org 29148", 5, None)

    assert calls == ["site:iso.org 29148"], "no keyword retry when on-domain results exist"
    assert [r["url"] for r in out] == ["https://www.iso.org/standard/72089.html"]


def test_nothing_on_domain_returns_empty_so_the_chain_can_fall_through(monkeypatch):
    monkeypatch.setattr(core, "searxng_search_api", lambda q, c, time_filter=None: [_r("https://elsewhere.org/a")])
    assert core._call_provider("searxng", "site:lucide.dev license", 5, None) == []


def test_queries_without_site_are_untouched(monkeypatch):
    rows = [_r("https://a.example/"), _r("https://b.example/")]
    monkeypatch.setattr(core, "searxng_search_api", lambda q, c, time_filter=None: list(rows))
    assert core._call_provider("searxng", "plain query", 5, None) == rows


def test_no_results_message_explains_the_site_scope(monkeypatch):
    monkeypatch.setattr(core, "_get_search_settings", lambda: {"search_provider": "searxng", "search_fallback_chain": ["disabled"]})
    monkeypatch.setattr(core, "_get_result_count", lambda: 3)
    monkeypatch.setattr(core, "_call_provider", lambda *a, **k: [])
    msg = core.comprehensive_web_search("site:lucide.dev license", max_pages=2)
    assert "site:lucide.dev" in msg and "web_fetch" in msg


def _capture_searxng(monkeypatch, results=None):
    seen = []

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": results or []}

    def fake_get(url, **kwargs):
        seen.append(dict(kwargs["params"]))
        return _Response()

    monkeypatch.setattr(providers, "_get_search_instance", lambda: "http://searx.test")
    monkeypatch.setattr(providers, "_get_search_settings", lambda: {})
    monkeypatch.setattr(providers.httpx, "get", fake_get)
    return seen


def test_site_query_with_year_filter_skips_the_news_category(monkeypatch):
    seen = _capture_searxng(monkeypatch, [{"title": "t", "url": "https://lucide.dev/license", "content": ""}])
    providers.searxng_search_api("site:lucide.dev license", count=5, time_filter="year")
    assert len(seen) == 1, "one request: no news attempt first"
    assert seen[0]["categories"] == "general"


def test_year_filter_alone_is_not_a_news_signal(monkeypatch):
    seen = _capture_searxng(monkeypatch, [{"title": "t", "url": "https://x.test/", "content": ""}])
    providers.searxng_search_api("ISO 29148 requirements traceability", count=5, time_filter="year")
    assert len(seen) == 1 and seen[0]["categories"] == "general"


def test_week_filter_and_news_words_still_use_the_news_category(monkeypatch):
    seen = _capture_searxng(monkeypatch, [{"title": "t", "url": "https://x.test/", "content": ""}])
    providers.searxng_search_api("canada election", count=5, time_filter="week")
    assert seen[0]["categories"] == "news" and seen[0]["time_range"] == "week"
    seen.clear()
    providers.searxng_search_api("canada latest news", count=5, time_filter=None)
    assert seen[0]["categories"] == "news"
