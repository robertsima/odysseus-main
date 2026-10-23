"""Changes and Commits scroll their lists inside the Workbench window.

Bug: in the Agamemnon style the tab panel grew to its content, so a 50-commit
list ran past the window's bottom edge and bringing Load more into view scrolled
the whole window body, taking the tabs and scope header out of sight.
"""
from __future__ import annotations

import json

import pytest

from tests.helpers.static_app import open_workbench, settle

pytestmark = pytest.mark.browser

COMMITS = [{"sha": f"{i:040x}", "subject": f"Commit number {i}", "author": "Alex"} for i in range(50)]


@pytest.mark.parametrize("style", ["agamemnon", "classic"])
@pytest.mark.parametrize("width", [1440, 900])
def test_load_more_scrolls_the_list_not_the_window(open_app, style, width):
    page = open_app(width, style=style)
    page.route("**/api/workbench/repo/commits*", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"commits": COMMITS, "has_more": True, "total": 65, "next_offset": 50})))
    open_workbench(page, activity=False)
    page.click("#wb-tab-commits")
    page.wait_for_selector('#wb-commits [data-wb-act="more-commits"]', state="attached")
    page.evaluate("document.querySelector('#wb-commits [data-wb-act=\"more-commits\"]').scrollIntoView()")
    settle(page, ".workbench-modal-content")
    box = page.evaluate(
        "(() => { const m = document.querySelector('#workbench-modal .modal-content').getBoundingClientRect();"
        " const t = document.querySelector('#wb-tab-commits').getBoundingClientRect();"
        " const s = document.querySelector('#wb-commits .wb-scope').getBoundingClientRect();"
        " const b = document.querySelector('#wb-commits [data-wb-act=\"more-commits\"]').getBoundingClientRect();"
        " return {mTop: m.top, mBottom: m.bottom, tabTop: t.top, tabBottom: t.bottom, scopeTop: s.top, moreBottom: b.bottom}; })()")
    assert box["tabTop"] >= box["mTop"] and box["tabBottom"] <= box["mBottom"], box
    assert box["scopeTop"] >= box["mTop"], box
    assert box["moreBottom"] <= box["mBottom"] + 1, box
