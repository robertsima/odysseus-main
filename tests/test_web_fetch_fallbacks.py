"""web_fetch on the failures the logs keep showing: a bot-blocked 403 and a
JavaScript app that returns 200 with an empty body."""
import asyncio

import src.search.content as content_mod
from src.agent_tools.web_tools import WebFetchTool, _fetch_error_hint


def _run(monkeypatch, result):
    monkeypatch.setattr(content_mod, "fetch_webpage_content", lambda url, **kw: result)
    return asyncio.run(WebFetchTool().execute('{"url": "https://example.test/page"}', {}))


def test_403_error_tells_the_agent_what_to_do_instead(monkeypatch):
    out = _run(monkeypatch, {"content": "", "title": "", "error": "HTTP 403: HTTP 403 for https://example.test/page"})
    assert out["exit_code"] == 1
    assert "HTTP 403" in out["error"]
    assert "browser tool" in out["error"] and "web_search" in out["error"]


def test_js_rendered_page_returns_its_metadata_as_partial_content(monkeypatch):
    out = _run(monkeypatch, {
        "content": "", "error": "", "title": "Phosphor Icons",
        "meta_description": "A flexible icon family for interfaces, diagrams, presentations.",
        "js_rendered": True,
    })
    assert out["exit_code"] == 0
    assert out["output"].startswith("[partial content:")
    assert "# Phosphor Icons" in out["output"]
    assert "Description: A flexible icon family" in out["output"]
    assert "Source: https://example.test/page" in out["output"]


def test_truly_empty_page_still_fails_with_a_next_step(monkeypatch):
    out = _run(monkeypatch, {"content": "", "error": "", "title": "", "meta_description": ""})
    assert out["exit_code"] == 1
    assert "no readable text content" in out["error"]
    assert "browser tool" in out["error"]


def test_error_hints_cover_the_recurring_statuses():
    assert "browser tool" in _fetch_error_hint("HTTP 403: forbidden")
    assert "gone" in _fetch_error_hint("HTTP 404: not found")
    assert "rate limited" in _fetch_error_hint("HTTP 429: too many")
    assert _fetch_error_hint("NetworkError: dns") == ""
