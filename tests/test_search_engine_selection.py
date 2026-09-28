"""SearXNG engine selection, relevance gate precision, and GitHub issue search.

The 2026-09-27 22:30-22:55 bundle (server already on searxng `latest`):
366 SearXNG requests from ~5 research workers; brave reported "too many
requests" on 365, duckduckgo timed out on 365, google cse was suspended on
346 -- although Odysseus pinned ``engines=bing,mojeek,presearch``. SearXNG
adds every engine of a requested category to an explicit engine list, so
the pin plus ``categories=general`` queried all defaults every time. The
relevance gate then kept 0 of 5 rows on 132 of 189 checks, and 84 queries
were ``site:github.com/<org>/<repo>/issues ...``, which SearXNG cannot honour.
"""

import logging

import httpx
import pytest

from services.search import core, providers, resilience
from services.search.query import github_issue_scope


def _row(url, title="", snippet="", engine=""):
    row = {"url": url, "title": title, "snippet": snippet}
    if engine:
        row["engine"] = engine
    return row


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


def _capture_searxng(monkeypatch, payload=None):
    seen = []

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload if payload is not None else {"results": [
                {"title": "t", "url": "https://x.test/", "content": "", "engines": ["bing"]}
            ]}

    def fake_get(url, **kwargs):
        seen.append(dict(kwargs["params"]))
        return _Response()

    monkeypatch.setattr(providers, "_get_search_instance", lambda: "http://searx.test")
    monkeypatch.setattr(providers, "_get_search_settings", lambda: {})
    monkeypatch.setattr(providers.httpx, "get", fake_get)
    return seen


# ----------------------------------------------------------------------
# SearXNG request shape
# ----------------------------------------------------------------------
def test_pinned_engines_are_sent_without_a_category(monkeypatch):
    monkeypatch.delenv("SEARXNG_GENERAL_ENGINES", raising=False)
    seen = _capture_searxng(monkeypatch)
    providers.searxng_search_api("pgvector hybrid search", count=5)
    # Bing answered in the 2026-09-27 22:55 bundle but its rows were junk;
    # qwant served a CAPTCHA from the NAS on 2026-09-28.
    assert seen[0]["engines"] == "yahoo,brave,wikipedia"
    assert not {"bing", "qwant"} & set(seen[0]["engines"].split(","))
    assert "categories" not in seen[0], (
        "categories=general would make SearXNG add every default general engine to the pin"
    )


def test_engine_pin_is_configurable_and_can_be_turned_off(monkeypatch):
    seen = _capture_searxng(monkeypatch)
    monkeypatch.setenv("SEARXNG_GENERAL_ENGINES", " bing , qwant,bing ")
    providers.searxng_search_api("pgvector hybrid search", count=5)
    assert seen[-1]["engines"] == "bing,qwant"

    monkeypatch.setenv("SEARXNG_GENERAL_ENGINES", "")
    providers.searxng_search_api("pgvector hybrid search", count=5)
    assert "engines" not in seen[-1] and seen[-1]["categories"] == "general"


def test_inactive_or_removed_engines_are_dropped_from_the_pin(monkeypatch, caplog):
    monkeypatch.setattr(providers, "_warned_unavailable", set())
    monkeypatch.setenv("SEARXNG_GENERAL_ENGINES", "startpage,yahoo,mojeek,presearch,qwant")
    seen = _capture_searxng(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="services.search.providers"):
        providers.searxng_search_api("pgvector hybrid search", count=5)
        providers.searxng_search_api("pgvector hybrid search again", count=5)
    assert seen[0]["engines"] == "yahoo,qwant"
    warned = [r.getMessage() for r in caplog.records if "inactive or removed" in r.getMessage()]
    assert len(warned) == 3, "one warning per engine name, not per request"


def test_news_fallback_to_general_uses_the_new_pin(monkeypatch):
    monkeypatch.delenv("SEARXNG_GENERAL_ENGINES", raising=False)
    seen = _capture_searxng(monkeypatch, {"results": []})
    providers.searxng_search_api("canada election news", count=5)
    assert seen[0]["categories"] == "news"
    # Three words: could be an article title, so wikipedia is asked too.
    assert seen[1]["engines"] == "yahoo,brave,wikipedia" and "categories" not in seen[1]


def test_rate_limited_engines_cool_down_one_by_one(monkeypatch, clock):
    monkeypatch.delenv("SEARXNG_GENERAL_ENGINES", raising=False)
    seen = _capture_searxng(monkeypatch, {
        "results": [{"title": "t", "url": "https://x.test/", "content": "", "engines": ["wikipedia"]}],
        "unresponsive_engines": [["yahoo", "HTTP error 429"]],
    })
    providers.searxng_search_api("q one", count=5)
    providers.searxng_search_api("q two", count=5)
    assert seen[1]["engines"] == "brave,wikipedia"


def test_wikipedia_is_only_asked_for_entity_like_queries(monkeypatch):
    # It looks up one article by exact title; a keyword list is never a title
    # (0 rows on all 3 asks in the 2026-09-28 bundle).
    monkeypatch.delenv("SEARXNG_GENERAL_ENGINES", raising=False)
    seen = _capture_searxng(monkeypatch)
    providers.searxng_search_api("pgvector", count=5)
    providers.searxng_search_api("Retrieval-augmented generation", count=5)
    providers.searxng_search_api("pgvector hybrid search benchmark recall latency", count=5)
    providers.searxng_search_api("site:pgvector.dev hnsw", count=5)
    assert seen[0]["engines"] == "yahoo,brave,wikipedia"
    assert seen[1]["engines"] == "yahoo,brave,wikipedia"
    assert seen[2]["engines"] == "yahoo,brave"
    assert seen[3]["engines"] == "yahoo,brave"


def test_an_operator_pin_of_wikipedia_alone_is_honoured_for_titles(monkeypatch):
    monkeypatch.setenv("SEARXNG_GENERAL_ENGINES", "wikipedia")
    seen = _capture_searxng(monkeypatch)
    providers.searxng_search_api("pgvector", count=5)
    providers.searxng_search_api("pgvector hybrid search benchmark recall latency", count=5)
    assert seen[0]["engines"] == "wikipedia"
    assert "engines" not in seen[1] and seen[1]["categories"] == "general"


def test_wikipedia_alone_does_not_count_as_a_working_pin(monkeypatch, clock):
    monkeypatch.delenv("SEARXNG_GENERAL_ENGINES", raising=False)
    seen = _capture_searxng(monkeypatch, {
        "results": [{"title": "t", "url": "https://x.test/", "content": ""}],
        "unresponsive_engines": [["yahoo", "CAPTCHA"], ["brave", "too many requests"]],
    })
    providers.searxng_search_api("pgvector", count=5)
    providers.searxng_search_api("pgvector", count=5)
    assert seen[1]["categories"] == "general" and "engines" not in seen[1]


def test_infobox_hits_become_rows(monkeypatch):
    # wikipedia's default display_type is ["infobox"]: its hit is in
    # `infoboxes`, not `results`.
    _capture_searxng(monkeypatch, {
        "results": [{"title": "pgvector on GitHub", "url": "https://github.com/pgvector/pgvector",
                     "content": "Open-source vector similarity search", "engines": ["brave"]}],
        "infoboxes": [
            {"infobox": "Pgvector", "id": "https://en.wikipedia.org/wiki/Pgvector",
             "content": "pgvector is a PostgreSQL extension", "engine": "wikipedia",
             "urls": [{"title": "Wikipedia", "url": "https://en.wikipedia.org/wiki/Pgvector"}]},
            {"infobox": "No url", "content": "dropped"},
            "junk",
        ],
    })
    rows = providers.searxng_search_api("pgvector", count=5)
    assert [r["url"] for r in rows] == [
        "https://en.wikipedia.org/wiki/Pgvector", "https://github.com/pgvector/pgvector",
    ]
    assert rows[0]["title"] == "Pgvector" and rows[0]["engine"] == "wikipedia"
    assert rows[0]["snippet"].startswith("pgvector is a PostgreSQL extension")


def test_an_infobox_alone_is_an_answer(monkeypatch):
    seen = _capture_searxng(monkeypatch, {
        "results": [],
        "infoboxes": [{"infobox": "Pgvector", "id": "wd:Q1",
                       "urls": [{"title": "Wikipedia", "url": "https://en.wikipedia.org/wiki/Pgvector"}],
                       "engines": ["wikidata"]}],
    })
    rows = providers.searxng_search_api("pgvector", count=5)
    assert len(seen) == 1, "no retry ladder when the infobox answered"
    assert rows[0]["url"] == "https://en.wikipedia.org/wiki/Pgvector"


def test_news_queries_keep_the_news_category(monkeypatch):
    seen = _capture_searxng(monkeypatch)
    providers.searxng_search_api("canada election", count=5, time_filter="week")
    assert seen[0]["categories"] == "news" and "engines" not in seen[0]


def test_last_resort_retry_asks_searxng_defaults(monkeypatch):
    seen = _capture_searxng(monkeypatch, {"results": []})
    providers.searxng_search_api("pgvector hybrid search", count=5)
    assert seen[0]["engines"] and "language" in seen[0]
    assert seen[1]["engines"] and "language" not in seen[1]
    assert "engines" not in seen[2] and seen[2]["categories"] == "general"


def test_rows_carry_their_engine_and_the_pool_is_not_cut_to_five(monkeypatch):
    payload = {"results": [
        {"title": f"t{i}", "url": f"https://x.test/{i}", "content": "", "engines": ["bing", "yahoo"]}
        for i in range(25)
    ]}
    _capture_searxng(monkeypatch, payload)
    rows = providers.searxng_search_api("pgvector hybrid search", count=20)
    assert len(rows) == 20
    assert rows[0]["engine"] == "bing,yahoo"


def test_suspended_pinned_engine_is_left_out_for_a_cooldown(monkeypatch, clock):
    monkeypatch.delenv("SEARXNG_GENERAL_ENGINES", raising=False)
    payload = {
        "results": [{"title": "t", "url": "https://x.test/", "content": "", "engines": ["yahoo"]}],
        "unresponsive_engines": [
            ["brave", "Suspended: too many requests"],
            ["qwant", "Suspended: CAPTCHA"],  # not pinned: ignored
        ],
    }
    seen = _capture_searxng(monkeypatch, payload)
    providers.searxng_search_api("first query", count=5)
    providers.searxng_search_api("second query", count=5)
    assert seen[0]["engines"] == "yahoo,brave,wikipedia"
    assert seen[1]["engines"] == "yahoo,wikipedia"
    assert not resilience.cooldowns.is_cooling("searxng engine qwant")

    clock.t += providers._engine_cooldown_seconds() + 1
    providers.searxng_search_api("third query", count=5)
    assert seen[2]["engines"] == "yahoo,brave,wikipedia"


def test_a_plain_timeout_does_not_cool_an_engine(monkeypatch, clock):
    monkeypatch.delenv("SEARXNG_GENERAL_ENGINES", raising=False)
    payload = {
        "results": [{"title": "t", "url": "https://x.test/", "content": ""}],
        "unresponsive_engines": [["brave", "timeout"]],
    }
    seen = _capture_searxng(monkeypatch, payload)
    providers.searxng_search_api("q one", count=5)
    providers.searxng_search_api("q two", count=5)
    assert seen[1]["engines"] == "yahoo,brave,wikipedia"


def test_when_every_pinned_engine_cools_searxng_defaults_are_used(monkeypatch, clock):
    monkeypatch.setenv("SEARXNG_GENERAL_ENGINES", "bing")
    payload = {
        "results": [{"title": "t", "url": "https://x.test/", "content": ""}],
        "unresponsive_engines": [["bing", "CAPTCHA"]],
    }
    seen = _capture_searxng(monkeypatch, payload)
    providers.searxng_search_api("q one", count=5)
    providers.searxng_search_api("q two", count=5)
    assert seen[1]["categories"] == "general" and "engines" not in seen[1]


# ----------------------------------------------------------------------
# Relevance gate
# ----------------------------------------------------------------------
def test_gate_folds_suffixes_and_trusts_identifier_tokens():
    # Real-engine rows for a query the production gate answered with 0/5.
    query = "LongMemEval benchmark temporal reasoning knowledge updates"
    rows = [
        _row("https://arxiv.org/abs/2410.10813",
             "[2410.10813] LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory"),
        _row("https://github.com/xiaowu0162/longmemeval",
             "GitHub - xiaowu0162/LongMemEval: Benchmarking Chat Assistants"),
    ]
    assert core._keep_relevant_results(query, rows) == rows


def test_gate_joins_identifier_spellings():
    rows = [_row("https://github.com/run-llama/llama_index/issues/14756",
                 "How to insert/delete document to/from VectorStoreIndex")]
    assert core._keep_relevant_results("delete_ref_doc LlamaIndex", rows) == rows


def test_scaffolding_words_are_not_subject_evidence():
    query = "GitHub issue vector database partial upsert failed inconsistent collection"
    junk = _row("https://github.com/someone/dotfiles/issues/3", "Fix zsh prompt · Issue #3", "")
    good = _row("https://github.com/qdrant/qdrant/issues/9",
                "Partial upsert leaves collection inconsistent", "vector database write failed")
    assert core._keep_relevant_results(query, [junk, good]) == [good]


def test_years_do_not_count_as_matches():
    query = "vector store stale source documents RAG 2025 2026"
    junk = _row("https://news.example/2026/recap", "2025 in review", "Everything from 2026")
    assert core._keep_relevant_results(query, [junk]) == []


def test_one_hit_is_enough_inside_the_requested_repository():
    query = "site:github.com/langchain-ai/langchain/issues vector store delete update stale source"
    inside = _row("https://github.com/langchain-ai/langchain/issues/1", "Chroma delete by id", "")
    elsewhere = _row("https://github.com/other/thing/issues/2", "delete button", "")
    assert core._keep_relevant_results(query, [inside, elsewhere]) == [inside]


def test_long_query_one_ordinary_word_is_still_rejected():
    query = "offline data synchronization field service utilities mapping conflict resolution"
    dictionary = _row("https://www.merriam-webster.com/dictionary/offline",
                      "Offline Definition & Meaning", "The meaning of OFFLINE is not connected")
    assert core._keep_relevant_results(query, [dictionary]) == []


def test_gate_logs_dropped_hosts_and_engines(caplog):
    caplog.set_level(logging.INFO, logger="services.search.core")
    query = "offline data synchronization field service utilities mapping conflict resolution"
    rows = [_row("https://www.merriam-webster.com/dictionary/offline", "Offline", "", engine="bing")]
    core._keep_relevant_results(query, rows)
    line = next(r.getMessage() for r in caplog.records if "Relevance gate" in r.getMessage())
    assert "kept 0/1" in line and "www.merriam-webster.com[bing]:1" in line


# ----------------------------------------------------------------------
# Provider call: pool, site scope, trimming, labels
# ----------------------------------------------------------------------
def test_searxng_is_asked_for_a_larger_pool_and_the_chain_trims(monkeypatch):
    asked = []

    def fake_searxng(query, count, time_filter=None):
        asked.append(count)
        return [_row(f"https://docs.example/pgvector/{i}", "pgvector hybrid search") for i in range(count)]

    monkeypatch.setattr(core, "searxng_search_api", fake_searxng)
    results, attempts = core._run_chain("pgvector hybrid search", 5, None, ["searxng"])
    assert asked == [20]
    assert len(results) == 5 and attempts == {"searxng": "ok (5)"}


def test_direct_provider_calls_keep_their_count(monkeypatch):
    # /api/search/query and deep research call _call_provider themselves.
    asked = []
    monkeypatch.setattr(core, "searxng_search_api",
                        lambda q, count, time_filter=None: asked.append(count) or [])
    core._call_provider("searxng", "pgvector hybrid search", 5)
    assert asked == [5]


def test_site_retry_keeps_the_repository_and_prefers_rows_inside_it(monkeypatch):
    calls = []

    # SearXNG sends the keyword form itself (test_search_site_operator); a
    # provider that is sent the operator gets the keyword retry.
    def fake_ddg(query, count, time_filter=None):
        calls.append(query)
        if query.startswith("site:"):
            return [_row("https://medium.com/x")]
        return [_row("https://github.com/other/repo/issues/1"),
                _row("https://github.com/langchain-ai/langchain/issues/2")]

    monkeypatch.setattr(core, "duckduckgo_search", fake_ddg)
    out = core._call_provider("duckduckgo", "site:github.com/langchain-ai/langchain/issues stale vectors", 5)
    assert calls[1] == "stale vectors langchain-ai/langchain github.com"
    assert [r["url"] for r in out] == [
        "https://github.com/langchain-ai/langchain/issues/2",
        "https://github.com/other/repo/issues/1",
    ]


def test_rows_rejected_by_the_gate_are_reported_as_irrelevant(monkeypatch):
    monkeypatch.setattr(core, "_get_search_settings",
                        lambda: {"search_provider": "searxng", "search_fallback_chain": ["disabled"]})
    monkeypatch.setattr(core, "_get_result_count", lambda: 3)
    monkeypatch.setattr(core, "_call_provider", lambda *a, **k: [
        _row("https://travel.example/sapporo", "Sapporo travel guide", "Hotels")
    ])
    msg = core.comprehensive_web_search("robertsima/odysseus-main poller", max_pages=2)
    assert msg.startswith("No search results found")
    assert "searxng:irrelevant (0/1)" in msg
    assert "3-6 specific terms" in msg


# ----------------------------------------------------------------------
# GitHub issue scope
# ----------------------------------------------------------------------
@pytest.mark.parametrize("query, expected", [
    ("site:github.com/langchain-ai/langchain/issues vector store delete update stale source documents RAG 2025 2026",
     {"repo": "langchain-ai/langchain", "kind": "issue",
      "terms": ["vector", "store", "delete", "update", "stale"]}),
    ("site:github.com/qdrant/qdrant/pulls payload filter",
     {"repo": "qdrant/qdrant", "kind": "pr", "terms": ["payload", "filter"]}),
    ("site:github.com/issues LangChain index cleanup full incremental stale",
     {"repo": None, "kind": "issue", "terms": ["LangChain", "index", "cleanup", "full", "incremental"]}),
    ("site:github.com RAG ACL metadata permissions drift issue",
     {"repo": None, "kind": "issue", "terms": ["RAG", "ACL", "metadata", "permissions", "drift"]}),
    ("github.com/continuedev/continue indexing codebase context retrieval",
     {"repo": "continuedev/continue", "kind": "issue",
      "terms": ["indexing", "codebase", "context", "retrieval"]}),
    ("GitHub mem0 issue duplicate memories update",
     {"repo": None, "kind": "issue", "terms": ["mem0", "duplicate", "memories", "update"]}),
])
def test_github_issue_scope_recognises_issue_queries(query, expected):
    assert github_issue_scope(query) == expected


@pytest.mark.parametrize("query", [
    "site:github.com entity resolution benchmark source code",   # repos/code, no issue intent
    "vector database stale embeddings",                           # nothing GitHub about it
    "site:docs.stripe.com terminal offline issues",              # another site
    "site:github.com/org/repo/issues 2025 github issues",        # no subject terms left
])
def test_github_issue_scope_ignores_other_queries(query):
    assert github_issue_scope(query) is None


# ----------------------------------------------------------------------
# GitHub issue search provider
# ----------------------------------------------------------------------
class _GHResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"items": []}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._payload


def _issue(n, title="Stale vectors after delete", repo="langchain-ai/langchain"):
    return {
        "number": n, "title": title, "state": "open",
        "html_url": f"https://github.com/{repo}/issues/{n}",
        "repository_url": f"https://api.github.com/repos/{repo}",
        "body": "Deleting a source document leaves its chunks in the vector store.",
        "updated_at": "2026-08-01T10:00:00Z",
    }


def test_github_issue_search_builds_a_scoped_query_and_retries_shorter(monkeypatch, clock):
    monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)
    sent = []

    def fake_get(url, **kwargs):
        sent.append((url, dict(kwargs["params"]), dict(kwargs["headers"])))
        if len(sent) == 1:
            return _GHResponse(payload={"items": []})
        return _GHResponse(payload={"items": [_issue(1), _issue(2)]})

    monkeypatch.setattr(providers.httpx, "get", fake_get)
    rows = providers.github_issue_search(
        "site:github.com/langchain-ai/langchain/issues vector store delete update stale source", 5
    )
    assert sent[0][0] == "https://api.github.com/search/issues"
    assert sent[0][1]["q"] == "vector store delete update stale repo:langchain-ai/langchain is:issue"
    assert sent[1][1]["q"] == "vector store delete repo:langchain-ai/langchain is:issue"
    assert "Authorization" not in sent[0][2]
    assert [r["url"] for r in rows] == [
        "https://github.com/langchain-ai/langchain/issues/1",
        "https://github.com/langchain-ai/langchain/issues/2",
    ]
    assert rows[0]["title"] == "Stale vectors after delete · Issue #1 · langchain-ai/langchain (open)"
    assert rows[0]["age"] == "2026-08-01" and rows[0]["engine"] == "github"


def test_github_issue_search_uses_a_valid_public_token(monkeypatch):
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_" + "a" * 36)
    monkeypatch.delenv("GITHUB_HOST", raising=False)
    sent = []
    monkeypatch.setattr(providers.httpx, "get",
                        lambda url, **kw: sent.append(kw["headers"]) or _GHResponse(payload={"items": [_issue(1)]}))
    providers.github_issue_search("site:github.com/o/r/issues stale vectors", 5)
    assert sent[0]["Authorization"] == "Bearer ghp_" + "a" * 36


def test_github_rate_limit_cools_the_provider_down(monkeypatch, clock):
    calls = []

    def limited(url, **kwargs):
        calls.append(1)
        return _GHResponse(403, {"message": "API rate limit exceeded"},
                           {"x-ratelimit-remaining": "0", "retry-after": "42"})

    monkeypatch.setattr(providers.httpx, "get", limited)
    assert providers.github_issue_search("site:github.com/o/r/issues stale vectors", 5) == []
    assert providers.github_issue_search("site:github.com/o/r/issues other words", 5) == []
    assert calls == [1], "no second request while cooling down"
    assert 41 <= resilience.cooldowns.remaining("github issue search") <= 42


def test_github_issue_search_can_be_disabled(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_GITHUB_ISSUE_SEARCH", "0")
    monkeypatch.setattr(providers.httpx, "get", lambda *a, **k: pytest.fail("must not call GitHub"))
    assert providers.github_issue_search("site:github.com/o/r/issues stale vectors", 5) == []


# ----------------------------------------------------------------------
# GitHub step in the provider chain
# ----------------------------------------------------------------------
@pytest.fixture
def searxng_only(monkeypatch):
    monkeypatch.setattr(core, "_get_search_settings",
                        lambda: {"search_provider": "searxng", "search_fallback_chain": ["disabled"]})
    monkeypatch.setattr(core, "_get_result_count", lambda: 3)
    monkeypatch.setattr(core, "fetch_webpage_content",
                        lambda url, *a, **k: {"success": True, "url": url, "title": "t", "content": "body"})
    monkeypatch.delenv("ODYSSEUS_GITHUB_ISSUE_SEARCH", raising=False)


def test_github_scoped_query_asks_github_first_and_skips_searxng(monkeypatch, searxng_only):
    calls = []

    def call_provider(provider, query, count, time_filter=None):
        calls.append(provider)
        if provider == core.GITHUB_ISSUES:
            # Short body: the gate would reject it, but GitHub rows skip the gate.
            return [_row("https://github.com/o/r/issues/7", "Crash on start · Issue #7 · o/r (open)")]
        return []

    monkeypatch.setattr(core, "_call_provider", call_provider)
    _, sources = core.comprehensive_web_search(
        "site:github.com/o/r/issues vector store stale deleted documents chunks", return_sources=True
    )
    assert calls == [core.GITHUB_ISSUES]
    assert [s["url"] for s in sources] == ["https://github.com/o/r/issues/7"]


def test_github_step_is_not_repeated_on_the_simplified_pass(monkeypatch, searxng_only):
    calls = []

    def call_provider(provider, query, count, time_filter=None):
        calls.append(provider)
        return []

    monkeypatch.setattr(core, "_call_provider", call_provider)
    msg = core.comprehensive_web_search(
        "site:github.com/o/r/issues vector store stale deleted documents chunks official documentation 2026",
        max_pages=2,
    )
    assert calls == [core.GITHUB_ISSUES, "searxng", "searxng"]
    assert msg.startswith("No search results found")


def test_plain_queries_do_not_touch_github(monkeypatch, searxng_only):
    calls = []
    monkeypatch.setattr(core, "_call_provider",
                        lambda provider, *a, **k: calls.append(provider) or [])
    core.comprehensive_web_search("pgvector hybrid search", max_pages=2)
    assert core.GITHUB_ISSUES not in calls
