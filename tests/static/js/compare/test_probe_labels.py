"""The compare view's pre-flight check (static/js/compare/selector.js): the
names it lists for search providers come from the server's provider list and
are shown as text, never as markup.
"""
from __future__ import annotations

import pytest

from tests.helpers.static_app import expect

pytestmark = pytest.mark.browser

PAYLOAD = "<img src=x onerror=window.__xss=1>"


def test_a_search_provider_named_with_markup_is_listed_as_text(open_app):
    page = open_app(1440, chat=False)
    page.route("**/api/models*", lambda route: route.fulfill(json={"items": [
        {"url": "http://models.test/v1", "endpoint_id": "e1", "endpoint_name": "Test",
         "models": ["chat-alpha", "chat-beta"]}]}))
    page.route("**/api/search/providers", lambda route: route.fulfill(json=[
        {"id": PAYLOAD, "label": PAYLOAD, "available": True}]))
    page.route("**/api/probe-selected", lambda route: route.fulfill(json={
        "results": [{"status": "ok", "model": "chat-alpha", "latency_ms": 5}]}))
    # Hold the provider check open so its rows stay on screen.
    held = []
    page.route("**/api/search/query", lambda route: held.append(route))
    page.evaluate("document.getElementById('tool-compare-btn').click()")
    page.click(".compare-mode-tab:has-text('Research')")
    page.wait_for_selector(".cmp-prov-select option", state="attached")
    page.click(".research-start-btn")
    name = page.locator(".compare-probe-row .compare-probe-name", has_text="onerror")
    # One row per model slot, each showing the provider's name as plain text.
    expect(name).to_have_text([PAYLOAD, PAYLOAD])
    assert page.locator(".compare-probe-row img").count() == 0
    assert page.evaluate("window.__xss") is None
    for route in held:
        route.fulfill(json={"results": []})
