"""The Assign sheet and the Agents menu rows in a real page.

What only a browser shows: the sheet stays inside the viewport and never
scrolls sideways at phone width, a blocker's buttons are reachable and not
covered, and the menu rows fit with two task buttons each.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.helpers.static_app import LIGHT_THEME, ODYSSEUS_THEME, assert_usable, probe, settle

pytestmark = pytest.mark.browser

SHOTS = os.environ.get("ODYSSEUS_SHOT_DIR")


def shot(page, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def open_sheet(page, task: str = "") -> None:
    page.evaluate("window.agentActions.openAssign({agent: 'Reader'})")
    page.wait_for_selector("#aa-overlay .aa-sheet")
    page.wait_for_function("document.querySelector('[data-aa-part=\"summary\"]').textContent.includes('Can read')")
    if task:
        page.fill('[data-aa-field="task"]', task)
        page.wait_for_selector('[data-aa-part="blocker"]:not([hidden])')
    settle(page, ".aa-dialog")


@pytest.mark.parametrize("width,theme,style", [
    (1440, ODYSSEUS_THEME, "agamemnon"), (1440, LIGHT_THEME, "classic"),
    (390, ODYSSEUS_THEME, "agamemnon"), (390, LIGHT_THEME, "classic"),
])
def test_sheet_with_a_blocker_fits_the_viewport_and_its_buttons_work(open_app, width, theme, style):
    page = open_app(width, theme=theme, style=style)
    open_sheet(page, "Explain how routing works in this repo")
    shot(page, f"agents-assign-blocker-{width}-{'light' if theme == LIGHT_THEME else 'dark'}-{style}")
    dialog = probe(page, ".aa-dialog")
    assert dialog["left"] >= -1 and dialog["right"] <= width + 1, dialog
    assert dialog["bottom"] <= page.viewport_size["height"] + 1, dialog
    assert not page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")
    assert not page.evaluate("(() => { const d = document.querySelector('.aa-dialog'); return d.scrollWidth > d.clientWidth + 1; })()")
    for selector in ('[data-aa-action="choose_workspace"]', '[data-aa-action="run_without_files"]', '[data-aa="go"]'):
        assert_usable(page, selector)
    page.click('[data-aa-action="run_without_files"]')
    page.wait_for_selector('[data-aa-part="blocker"][hidden]', state="attached")
    assert "Running without files" in page.inner_text('[data-aa-part="applied"]')


def test_sheet_without_a_blocker_shows_the_summary_and_results_line(open_app):
    page = open_app(1440)
    open_sheet(page)
    shot(page, "agents-assign-plain-1440-dark-agamemnon")
    assert page.is_hidden('[data-aa-part="blocker"]')
    assert "Results return" in page.inner_text('[data-aa-part="return"]')


@pytest.mark.parametrize("width,theme,style", [(1440, ODYSSEUS_THEME, "agamemnon"), (390, LIGHT_THEME, "classic")])
def test_menu_rows_offer_both_task_buttons_inside_the_menu(open_app, width, theme, style):
    page = open_app(width, theme=theme, style=style)
    page.evaluate("document.body.classList.add('composer-agent-mode')")
    page.evaluate("window.agentMenuModule.openMenu()")
    page.wait_for_selector("#agent-menu .agent-menu-row")
    settle(page, "#agent-menu")
    shot(page, f"agents-menu-{width}-{'light' if theme == LIGHT_THEME else 'dark'}-{style}")
    menu = probe(page, "#agent-menu")
    assert menu["left"] >= -1 and menu["right"] <= width + 1, menu
    rows = page.locator("#agent-menu .agent-menu-row")
    assert rows.count() == 2
    for i in range(rows.count()):
        labels = rows.nth(i).locator("[data-agent-act]").all_inner_texts()
        assert labels == ["Talk to agent", "Assign a task"]
        box = rows.nth(i).bounding_box()
        assert box["x"] >= menu["left"] - 1 and box["x"] + box["width"] <= menu["right"] + 1


@pytest.mark.parametrize("width,theme,style", [(1440, ODYSSEUS_THEME, "agamemnon"), (1440, LIGHT_THEME, "classic"), (390, ODYSSEUS_THEME, "classic")])
def test_panel_sheet_fits(open_app, width, theme, style):
    from tests.helpers.static_app import open_agents

    page = open_app(width, theme=theme, style=style)
    open_agents(page)
    page.click('[data-ag="launch"]')
    page.wait_for_selector("#ag-launch .aa-sheet")
    page.wait_for_selector('#ag-launch [data-aa-field="agent"] option[value="Reader"]', state="attached")
    page.select_option('#ag-launch [data-aa-field="agent"]', "Reader")
    page.fill('#ag-launch [data-aa-field="task"]', "Explain how routing works in this repo")
    page.wait_for_selector('#ag-launch [data-aa-part="blocker"]:not([hidden])')
    settle(page, "#ag-launch")
    tone = "light" if theme == LIGHT_THEME else "dark"
    shot(page, f"agents-panel-sheet-{width}-{tone}-{style}")
    box = probe(page, "#ag-launch")
    assert box["left"] >= -1 and box["right"] <= width + 1, box
    assert not page.evaluate("(() => { const d = document.querySelector('#ag-launch'); return d.scrollWidth > d.clientWidth + 1; })()")
