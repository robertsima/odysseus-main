"""GitHub issue rows are ranked by relevance to the query, not GitHub's order.

GitHub's "best match" order weighs term hits in comments and labels as much as
the title and ignores state and age, so a long-closed issue that mentions the
words somewhere in a long thread could come first. ``rank_github_issues``
scores title and body matches first, then open vs closed, recency and
reactions/comments, and uses GitHub's position only to break near-ties.
"""

from datetime import datetime, timezone

from services.search import core, providers

NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)
TERMS = ["stale", "vectors", "delete"]


def _item(n, title="Unrelated crash on startup", body="", state="open", updated="2026-09-01T00:00:00Z",
          reactions=0, comments=0, state_reason=None, repo="o/r"):
    return {
        "number": n,
        "title": title,
        "body": body,
        "state": state,
        "state_reason": state_reason,
        "html_url": f"https://github.com/{repo}/issues/{n}",
        "repository_url": f"https://api.github.com/repos/{repo}",
        "updated_at": updated,
        "reactions": {"total_count": reactions},
        "comments": comments,
    }


def _order(items, terms=TERMS):
    return [it["number"] for it in providers.rank_github_issues(items, terms, now=NOW)]


def test_title_match_beats_githubs_order():
    items = [
        _item(1, "Crash on startup", body="stale vectors delete appear in a log line"),
        _item(2, "Stale vectors remain after delete"),
    ]
    assert _order(items) == [2, 1]


def test_body_match_beats_no_match():
    items = [
        _item(1, "Question about the store"),
        _item(2, "Question about the store", body="After I delete a document its vectors stay stale."),
    ]
    assert _order(items) == [2, 1]


def test_open_ranks_above_closed_when_otherwise_equal():
    items = [
        _item(1, "Stale vectors after delete", state="closed", state_reason="not_planned"),
        _item(2, "Stale vectors after delete", state="open"),
    ]
    assert _order(items) == [2, 1]


def test_closed_as_completed_ranks_above_closed_as_not_planned():
    items = [
        _item(1, "Stale vectors after delete", state="closed", state_reason="not_planned"),
        _item(2, "Stale vectors after delete", state="closed", state_reason="completed"),
    ]
    assert _order(items) == [2, 1]


def test_recent_ranks_above_old_when_otherwise_equal():
    items = [
        _item(1, "Stale vectors after delete", updated="2023-01-10T00:00:00Z"),
        _item(2, "Stale vectors after delete", updated="2026-09-20T00:00:00Z"),
    ]
    assert _order(items) == [2, 1]


def test_discussed_issue_ranks_above_ignored_one():
    items = [
        _item(1, "Stale vectors after delete"),
        _item(2, "Stale vectors after delete", reactions=30, comments=12),
    ]
    assert _order(items) == [2, 1]


def test_signals_do_not_outweigh_a_title_match():
    # Popular, recent, open -- but about something else.
    items = [
        _item(1, "Crash on startup", state="open", updated="2026-09-27T00:00:00Z",
              reactions=500, comments=300),
        _item(2, "Stale vectors after delete", state="closed", state_reason="completed",
              updated="2025-01-01T00:00:00Z"),
    ]
    assert _order(items) == [2, 1]


def test_ties_keep_githubs_order():
    items = [_item(n, "Stale vectors after delete") for n in (5, 3, 9)]
    assert _order(items) == [5, 3, 9]


def test_prefix_and_phrase_matching():
    items = [
        _item(1, "Delete leaves vectors stale"),     # all words, other order
        _item(2, "Stale vectors after deleting"),    # in order, "deleting" starts with "delete"
    ]
    assert _order(items) == [2, 1]


def test_malformed_items_are_tolerated():
    items = [
        {"html_url": "https://github.com/o/r/issues/1", "title": None, "reactions": "x",
         "comments": "many", "updated_at": "not a date"},
        "junk",
        {"title": "no url"},
        _item(2, "Stale vectors after delete"),
    ]
    ranked = providers.rank_github_issues(items, TERMS, now=NOW)
    assert [it["html_url"] for it in ranked] == [
        "https://github.com/o/r/issues/2", "https://github.com/o/r/issues/1",
    ]


# ----------------------------------------------------------------------
# Through the provider and the chain
# ----------------------------------------------------------------------
class _GHResponse:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_issue_search_returns_ranked_rows_from_a_larger_page(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_GITHUB_ISSUE_SEARCH", raising=False)
    sent = []
    items = [_item(n, f"Unrelated thing {n}") for n in range(1, 9)]
    items.append(_item(42, "Stale vectors remain after delete"))

    def fake_get(url, **kwargs):
        sent.append(kwargs["params"])
        return _GHResponse({"items": items})

    monkeypatch.setattr(providers.httpx, "get", fake_get)
    rows = providers.github_issue_search("site:github.com/o/r/issues stale vectors delete", 3)
    assert sent[0]["per_page"] >= 20, "fetch more than needed so re-ranking has candidates"
    assert len(rows) == 3
    assert rows[0]["url"] == "https://github.com/o/r/issues/42"


def test_chain_keeps_the_github_order(monkeypatch):
    monkeypatch.setattr(core, "_get_search_settings",
                        lambda: {"search_provider": "searxng", "search_fallback_chain": ["disabled"]})
    monkeypatch.setattr(core, "_get_result_count", lambda: 3)
    monkeypatch.setattr(core, "fetch_webpage_content",
                        lambda url, *a, **k: {"success": True, "url": url, "title": "t", "content": "body"})
    monkeypatch.delenv("ODYSSEUS_GITHUB_ISSUE_SEARCH", raising=False)
    ranked = [
        {"url": "https://github.com/o/r/issues/7", "title": "Stale vectors after delete · Issue #7 · o/r (open)",
         "snippet": "", "age": "2026-09-01", "engine": "github"},
        # The generic ranker would move this one up: longer snippet, more
        # query words in it, newer.
        {"url": "https://github.com/o/r/issues/3", "title": "Other · Issue #3 · o/r (closed)",
         "snippet": "vector store stale deleted documents chunks " * 5, "age": "2026-09-27",
         "engine": "github"},
    ]

    def call_provider(provider, query, count, time_filter=None):
        return [dict(r) for r in ranked] if provider == core.GITHUB_ISSUES else []

    monkeypatch.setattr(core, "_call_provider", call_provider)
    _, sources = core.comprehensive_web_search(
        "site:github.com/o/r/issues vector store stale deleted documents chunks", return_sources=True,
    )
    assert [s["url"] for s in sources] == [r["url"] for r in ranked]


def test_mixed_rows_still_get_the_generic_ranking():
    rows = [
        {"url": "https://a.test/", "title": "nothing", "snippet": "", "engine": "yahoo"},
        {"url": "https://b.test/", "title": "pgvector hybrid search", "snippet": "", "engine": "github"},
    ]
    assert core._rank_rows("pgvector hybrid search", rows)[0]["url"] == "https://b.test/"
