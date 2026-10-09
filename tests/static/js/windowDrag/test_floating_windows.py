"""Floating tool windows (static/js/windowDrag.js, static/js/toolWindowZOrder.js):
moved by the title bar, stacked opaquely, and maximized."""
from __future__ import annotations

import pytest

from tests.helpers.static_app import drag, open_agents, open_workbench, probe, settle

pytestmark = pytest.mark.browser


def test_the_window_brought_forward_covers_the_other_one(open_app):
    page = open_app(1440)
    # Exercise the legacy two-section floating layout. Chat plus these two
    # tools now deliberately tiles instead (covered by workspaceLayout).
    page.evaluate("document.getElementById('chat-container').classList.add('hidden')")
    open_agents(page)
    open_workbench(page, activity=False)
    page.evaluate("document.body.classList.add('theme-frosted')")
    # Bring either window forward: its opaque content covers the other one.
    for front, other in [("#workbench-modal", "#agents-dashboard"), ("#agents-dashboard", "#workbench-modal")]:
        page.evaluate("(sel) => document.querySelector(sel + ' .modal-header')"
                      ".dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}))", front)
        a, b = probe(page, front + " .modal-content"), probe(page, other + " .modal-content")
        x, y = max(a["left"], b["left"]) + 100, max(a["top"], b["top"]) + 160
        assert page.evaluate("([x, y]) => document.elementFromPoint(x, y).closest('.modal')?.id", [x, y]) == front[1:]
        paint = page.evaluate("(sel) => { const cs = getComputedStyle(document.querySelector(sel + ' .modal-content'));"
                              " return {image: cs.backgroundImage, color: cs.backgroundColor}; }", front)
        assert paint["image"] == "none" and paint["color"].startswith("rgb("), (front, paint)
    # The Phalanx is in front now and covers the Workbench's close button.
    page.locator("#close-workbench-modal").dispatch_event("click")
    page.wait_for_function("document.getElementById('workbench-modal').classList.contains('hidden')")
    page.click("#ag-maximize")
    settle(page, ".agents-modal-content")
    assert probe(page, ".agents-modal-content")["width"] > 1440 * 0.7


@pytest.mark.parametrize("window,header,content", [
    ("agents", ".agents-window-header", ".agents-modal-content"),
    ("workbench", "#workbench-modal .modal-header", ".workbench-modal-content"),
])
def test_floating_window_moves_with_its_title_bar(open_app, window, header, content):
    page = open_app(1440)
    if window == "agents":
        open_agents(page)
    else:
        open_workbench(page, activity=False)
    before, bar = probe(page, content), probe(page, header)
    x, y = bar["left"] + 150, bar["top"] + 20
    drag(page, (x, y), (x + 120, y + 75))
    after = probe(page, content)
    assert after["left"] >= before["left"] + 100 and after["top"] >= before["top"] + 60, (before, after)
