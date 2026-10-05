"""Search regressions from the 2026-09-28 diagnostics bundle.

* brave reported "too many requests" at 07:33:24, 07:38:36 and 07:55:18 and
  was left out for a flat 300 s each time; SearXNG itself then listed it as
  "Suspended: too many requests". Search was effectively yahoo alone.
* ``site:docs.<vendor>`` lookups kept 2 of 7 rows after the domain filter.
* ``site:status.* ...`` was sent as the word ``status.*`` and the domain
  filter looked for a host literally named ``status.*`` (dropping
  status.withvector.com); the search reported nothing found.
* GitHub ranking and merge decisions were invisible in the log.
"""

import logging

import pytest

from services.search import core, providers, resilience
from services.search.query import normalize_site_wildcard


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        pass


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(resilience, "_now", c.now)
    monkeypatch.setattr(resilience, "_sleep", c.sleep)
    return c


def _capture_searxng(monkeypatch, payloads):
    """httpx.get stub: returns ``payloads(params)`` and records the params."""
    seen = []

    class _Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_get(url, **kwargs):
        params = dict(kwargs["params"])
        seen.append(params)
        return _Response(payloads(params))

    monkeypatch.setattr(providers, "_get_search_instance", lambda: "http://searx.test")
    monkeypatch.setattr(providers, "_get_search_settings", lambda: {})
    monkeypatch.setattr(providers.httpx, "get", fake_get)
    return seen


KEY = "searxng engine brave"


# ── exponential backoff per engine ─────────────────────────────────────────


def test_backoff_doubles_per_strike_up_to_the_cap(clock):
    cd = resilience.cooldowns
    got = []
    for _ in range(6):
        got.append(cd.backoff(KEY, 300, 3600, "too many requests"))
        clock.t += got[-1] + 1  # the engine is asked again after each pause
    assert got == [300, 600, 1200, 2400, 3600, 3600]
    assert cd.strikes(KEY) == 6


def test_reports_of_the_same_episode_are_one_strike(clock):
    cd = resilience.cooldowns
    assert cd.backoff(KEY, 300, 3600, "too many requests") == 300
    clock.t += 5
    # Parallel searches that were already in flight report it again.
    cd.backoff(KEY, 300, 3600, "too many requests")
    cd.backoff(KEY, 300, 3600, "too many requests")
    assert cd.strikes(KEY) == 1
    assert 294 <= cd.remaining(KEY) <= 296


def test_rows_from_the_engine_reset_the_backoff(clock):
    cd = resilience.cooldowns
    cd.backoff(KEY, 300, 3600)
    clock.t += 301
    cd.backoff(KEY, 300, 3600)
    assert cd.strikes(KEY) == 2
    providers._note_answering_engines([{"url": "https://a", "engines": ["brave", "yahoo"]}])
    assert cd.strikes(KEY) == 0
    clock.t += 601
    assert cd.backoff(KEY, 300, 3600) == 300


def test_a_long_quiet_spell_starts_over(clock):
    cd = resilience.cooldowns
    cd.backoff(KEY, 300, 3600)
    clock.t += 301
    cd.backoff(KEY, 300, 3600)
    clock.t += 3 * 3600 + 1
    assert cd.backoff(KEY, 300, 3600) == 300


def test_suspended_floor_extends_a_running_cooldown(clock):
    cd = resilience.cooldowns
    cd.backoff(KEY, 300, 3600, "too many requests")
    clock.t += 100
    assert cd.backoff(KEY, 300, 3600, "Suspended: too many requests", floor=3600) == 3600
    assert cd.strikes(KEY) == 1, "the same episode"
    assert 3599 <= cd.remaining(KEY) <= 3600


def test_zero_base_disables_cooldowns(clock):
    assert resilience.cooldowns.backoff(KEY, 0, 3600, floor=3600) == 0
    assert not resilience.cooldowns.is_cooling(KEY)


def test_searxng_reports_drive_the_backoff(monkeypatch, clock):
    monkeypatch.delenv("SEARXNG_GENERAL_ENGINES", raising=False)
    monkeypatch.delenv("SEARXNG_ENGINE_COOLDOWN_SECONDS", raising=False)
    report = {"unresponsive_engines": [["brave", "too many requests"]]}

    def payload(params):
        return {
            "results": [{"title": "t", "url": "https://x.test/", "content": "", "engines": ["yahoo"]}],
            **report,
        }

    seen = _capture_searxng(monkeypatch, payload)
    providers.searxng_search_api("pgvector hybrid search", count=5)
    assert 299 <= resilience.cooldowns.remaining(KEY) <= 300
    clock.t += 301
    providers.searxng_search_api("pgvector hnsw recall", count=5)
    assert "brave" in seen[-1]["engines"]
    assert 599 <= resilience.cooldowns.remaining(KEY) <= 600, "second strike doubles"

    # SearXNG's own suspension: at least an hour, however few strikes.
    clock.t += 601
    report["unresponsive_engines"] = [["brave", "Suspended: too many requests"]]
    providers.searxng_search_api("pgvector ivfflat lists", count=5)
    assert resilience.cooldowns.remaining(KEY) >= providers._engine_suspended_seconds() - 1


# ── site: result page 2 ────────────────────────────────────────────────────


def _row(url):
    return {"title": url, "url": url, "content": "", "engines": ["yahoo"]}


def test_site_query_with_few_on_domain_rows_asks_for_page_two(monkeypatch):
    def payload(params):
        if params.get("pageno") == 2:
            return {"results": [
                _row("https://docs.pinecone.io/guides/delete"),  # already seen
                _row("https://docs.pinecone.io/reference/delete"),
                _row("https://docs.pinecone.io/guides/metadata"),
                _row("https://www.youtube.com/watch?v=1"),
            ]}
        return {"results": [
            _row("https://docs.pinecone.io/guides/delete"),
            _row("https://community.pinecone.io/t/1"),
            _row("https://www.youtube.com/watch?v=2"),
        ]}

    seen = _capture_searxng(monkeypatch, payload)
    rows = core._call_provider("searxng", "site:docs.pinecone.io delete records", 20)

    assert [p.get("pageno") for p in seen] == [None, 2], "one extra request, no retry ladder"
    assert seen[1]["q"] == seen[0]["q"]
    assert [r["url"] for r in rows] == [
        "https://docs.pinecone.io/guides/delete",
        "https://docs.pinecone.io/reference/delete",
        "https://docs.pinecone.io/guides/metadata",
    ]


def test_enough_on_domain_rows_need_no_second_page(monkeypatch):
    seen = _capture_searxng(monkeypatch, lambda params: {"results": [
        _row(f"https://docs.pinecone.io/p{i}") for i in range(3)
    ]})
    rows = core._call_provider("searxng", "site:docs.pinecone.io delete records", 20)
    assert len(seen) == 1 and len(rows) == 3


def test_second_page_is_skipped_near_the_deadline(monkeypatch, clock):
    seen = _capture_searxng(monkeypatch, lambda params: {"results": [
        _row("https://docs.pinecone.io/guides/delete"), _row("https://elsewhere.test/")
    ]})
    with resilience.deadline_scope(core._SECOND_PAGE_MIN_SECONDS - 0.5):
        rows = core._call_provider("searxng", "site:docs.pinecone.io delete records", 20)
    assert len(seen) == 1
    assert [r["url"] for r in rows] == ["https://docs.pinecone.io/guides/delete"]


def test_a_failing_second_page_keeps_page_one(monkeypatch):
    calls = []

    def fake(query, count, time_filter=None, pageno=1):
        calls.append(pageno)
        if pageno == 2:
            raise RuntimeError("boom")
        return [{"title": "a", "url": "https://docs.pinecone.io/a", "snippet": ""}]

    monkeypatch.setattr(core, "searxng_search_api", fake)
    rows = core._call_provider("searxng", "site:docs.pinecone.io delete", 20)
    assert calls == [1, 2]
    assert [r["url"] for r in rows] == ["https://docs.pinecone.io/a"]


# ── wildcard site: values ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("site:status.* vector database deletion incident",
         "vector database deletion incident status"),
        ("site:docs.*.com delete records", "delete records docs"),
        ("status page outage site:*", "status page outage"),
    ],
)
def test_wildcard_site_values_are_dropped_with_a_note(query, expected):
    new, note = normalize_site_wildcard(query)
    assert new == expected
    assert "site:" not in new
    assert note and "wildcard" in note and "Searched without it" in note


@pytest.mark.parametrize("query", [
    "site:status.openai.com outage",
    "site:*.openai.com outage",
    "site:https://www.iso.org/standards 29148",
    "no operator here",
])
def test_usable_site_values_are_untouched(query):
    assert normalize_site_wildcard(query) == (query, None)


def test_comprehensive_search_explains_the_wildcard(monkeypatch):
    monkeypatch.setattr(core, "_get_search_settings",
                        lambda: {"search_provider": "searxng", "search_fallback_chain": ["disabled"]})
    monkeypatch.setattr(core, "_get_result_count", lambda: 3)
    asked = []

    def fake_call(provider, query, count, time_filter=None):
        asked.append(query)
        return []

    monkeypatch.setattr(core, "_call_provider", fake_call)
    msg = core.comprehensive_web_search("site:status.* vector database deletion incident", max_pages=2)
    assert asked and all("site:" not in q for q in asked)
    assert asked[0] == "vector database deletion incident status"
    assert msg.startswith("Note: site:status.* is not a usable site: scope")


def test_comprehensive_search_note_is_in_the_results(monkeypatch):
    monkeypatch.setattr(core, "_get_search_settings",
                        lambda: {"search_provider": "searxng", "search_fallback_chain": ["disabled"]})
    monkeypatch.setattr(core, "_get_result_count", lambda: 3)
    monkeypatch.setattr(core, "_call_provider", lambda *a, **k: [{
        "title": "Vector status incident deletion database",
        "url": "https://status.withvector.com/incidents/1",
        "snippet": "vector database deletion incident",
    }])
    monkeypatch.setattr(core, "fetch_webpage_content",
                        lambda url, *a, **k: {"success": False, "url": url, "title": "", "content": ""})
    out = core.comprehensive_web_search("site:status.* vector database deletion incident", max_pages=1)
    assert "status.withvector.com" in out
    assert "Note: site:status.* is not a usable site: scope" in out


# ── GitHub ranking / merge log lines ───────────────────────────────────────


class _GHResponse:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _item(n, title, repo="o/r", comments=0, reactions=0):
    return {
        "number": n, "title": title, "body": "", "state": "open",
        "html_url": f"https://github.com/{repo}/issues/{n}",
        "repository_url": f"https://api.github.com/repos/{repo}",
        "updated_at": "2026-09-01T00:00:00Z",
        "reactions": {"total_count": reactions}, "comments": comments,
    }


def test_issue_search_logs_the_top_ranked_repo_and_engagement(monkeypatch, caplog):
    monkeypatch.delenv("ODYSSEUS_GITHUB_ISSUE_SEARCH", raising=False)
    items = [_item(1, "Unrelated"), _item(42, "Stale vectors remain after delete", comments=7, reactions=5)]
    monkeypatch.setattr(providers.httpx, "get", lambda url, **kw: _GHResponse({"items": items}))
    with caplog.at_level(logging.INFO, logger="services.search.providers"):
        rows = providers.github_issue_search("site:github.com/o/r/issues stale vectors delete", 3)
    assert rows[0]["repo"] == "o/r" and rows[0]["engagement"] == 12
    line = next(r.getMessage() for r in caplog.records if "GitHub issue search" in r.getMessage())
    assert "ranked top: o/r #42 (engagement 12)" in line


def test_merge_logs_web_and_github_counts(caplog):
    web = [{"url": f"https://web.test/{i}", "title": "w", "snippet": ""} for i in range(5)]
    gh = [{"url": "https://github.com/a/b/issues/3", "title": "g", "snippet": "", "engine": "github",
           "repo": "a/b", "engagement": 0, "low_signal": True}]
    with caplog.at_level(logging.INFO, logger="services.search.core"):
        merged = core._merge_with_github(web, gh, 5)
    assert len(merged) == 5
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Merged"))
    assert line == (
        "Merged 4 web row(s) with 1 of 1 low-engagement GitHub row(s) "
        "(top GitHub: a/b #3 (engagement 0, low signal))"
    )
