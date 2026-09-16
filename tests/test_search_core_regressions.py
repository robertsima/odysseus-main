"""Focused regressions for provider call budgets and search relevance."""

from pathlib import Path

from services.search import core


def _row(url, title="", snippet=""):
    return {"url": url, "title": title, "snippet": snippet}


def test_comprehensive_search_calls_each_provider_once(monkeypatch):
    calls = []

    monkeypatch.setattr(
        core,
        "_get_search_settings",
        lambda: {"search_provider": "searxng", "search_fallback_chain": ["duckduckgo"]},
    )
    monkeypatch.setattr(core, "_get_result_count", lambda: 3)

    def call_provider(provider, query, count, time_filter=None):
        calls.append((provider, query))
        return []

    monkeypatch.setattr(core, "_call_provider", call_provider)
    context, sources = core.comprehensive_web_search("distinct subject", return_sources=True)

    assert sources == []
    assert calls == [
        ("searxng", "distinct subject"),
        ("duckduckgo", "distinct subject"),
    ]


def test_site_drift_queries_each_provider_once_per_distinct_query(monkeypatch):
    calls = []

    monkeypatch.setattr(
        core,
        "_get_search_settings",
        lambda: {"search_provider": "searxng", "search_fallback_chain": ["duckduckgo"]},
    )
    monkeypatch.setattr(core, "_get_result_count", lambda: 3)

    def off_domain(provider_name):
        def search(query, count, time_filter=None):
            calls.append((provider_name, query))
            return [_row("https://elsewhere.example/page", "Unrelated")]

        return search

    monkeypatch.setattr(core, "searxng_search_api", off_domain("searxng"))
    monkeypatch.setattr(core, "duckduckgo_search", off_domain("duckduckgo"))
    core.comprehensive_web_search("site:example.com distinctive subject", return_sources=True)

    assert calls == [
        ("searxng", "site:example.com distinctive subject"),
        ("searxng", "distinctive subject example.com"),
        ("duckduckgo", "site:example.com distinctive subject"),
        ("duckduckgo", "distinctive subject example.com"),
    ]


def test_unrelated_results_are_rejected_before_fetch(monkeypatch):
    fetches = []
    monkeypatch.setattr(core, "_get_search_settings", lambda: {"search_provider": "searxng"})
    monkeypatch.setattr(core, "_get_result_count", lambda: 3)
    monkeypatch.setattr(
        core,
        "_call_provider",
        lambda *args, **kwargs: [
            _row("https://travel.example/sapporo", "Sapporo travel guide", "Hotels and tours"),
            _row("https://tourism.example/japan", "Japan vacation", "Travel ideas"),
        ],
    )

    def fetch(url, *args, **kwargs):
        fetches.append(url)
        raise AssertionError("irrelevant search results must not be fetched")

    monkeypatch.setattr(core, "fetch_webpage_content", fetch)
    context, sources = core.comprehensive_web_search(
        "robertsima/odysseus-main poller", return_sources=True
    )

    assert sources == []
    assert fetches == []
    assert "No search results found" in context


def test_relevance_gate_keeps_a_token_overlap(monkeypatch):
    fetches = []
    monkeypatch.setattr(core, "_get_search_settings", lambda: {"search_provider": "searxng"})
    monkeypatch.setattr(core, "_get_result_count", lambda: 1)
    monkeypatch.setattr(
        core,
        "_call_provider",
        lambda *args, **kwargs: [
            _row(
                "https://docs.example/poller",
                "Polling worker",
                "A poller checks for new messages.",
            )
        ],
    )
    monkeypatch.setattr(
        core,
        "fetch_webpage_content",
        lambda url, *args, **kwargs: (
            fetches.append(url)
            or {"success": True, "url": url, "title": "Polling worker", "content": "details"}
        ),
    )

    context, sources = core.comprehensive_web_search("poller", return_sources=True)

    assert [source["url"] for source in sources] == ["https://docs.example/poller"]
    assert fetches == ["https://docs.example/poller"]


def test_default_duckduckgo_fallback_dependency_is_in_the_core_install():
    """DuckDuckGo is in the default fallback chain, so it cannot rely on the
    optional-extras image while still being advertised on a normal install."""
    root = Path(__file__).resolve().parents[1]
    core_requirements = {
        line.strip() for line in (root / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    optional_requirements = {
        line.strip() for line in (root / "requirements-optional.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "ddgs" in core_requirements
    assert "ddgs" not in optional_requirements
