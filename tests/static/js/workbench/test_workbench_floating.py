"""The floating Workbench window (static/js/workbench.js): tabs, run views,
feed controls and the branch editor at desktop, tablet and phone widths.

The regression that started these: every tab panel drawn at once, because a
``display:block!important`` beat ``.wb-panel.hidden``. 320 px stays where a
test checks the narrowest phone row (feed actions sharing one line).
"""
from __future__ import annotations

import re

import pytest

from tests.helpers.static_app import (
    assert_no_sideways_scroll, assert_usable, expect, open_workbench, probe, set_select, settle, type_into_editor,
    visible_panels,
)

pytestmark = pytest.mark.browser

RUN_SECTIONS = ".ag-run-summary,.wb-toolbar,.wb-activity,.ag-run-timeline,.ag-run-console,.ag-run-detail"
RUN_ORDER = ["ag-run-summary", "wb-toolbar", "wb-activity", "ag-run-timeline", "ag-run-console", "ag-run-detail"]
TABS = ["activity", "changes", "commits", "prs"]


def same_row(page, a: str, b: str) -> bool:
    return abs(probe(page, a)["top"] - probe(page, b)["top"]) <= 2


@pytest.mark.parametrize("width", [1440, 700, 390, 320])
def test_floating_workbench_draws_only_the_selected_tab(open_app, width):
    page = open_app(width)
    open_workbench(page)
    tabs = page.evaluate(
        "[...document.querySelectorAll('#workbench-modal [data-wb-tab]')].map(t => { const r = t.getBoundingClientRect();"
        " return {name: t.dataset.wbTab, height: r.height, left: r.left, right: r.right,"
        " visible: getComputedStyle(t).display !== 'none'}; })")
    assert [t["name"] for t in tabs] == TABS
    assert all(t["visible"] and t["height"] >= 44 and t["left"] >= 0 and t["right"] <= width + 1 for t in tabs), tabs
    if width <= 390:
        assert tabs[0]["left"] < tabs[1]["left"] < tabs[2]["left"] < tabs[3]["left"], tabs
    # The Activity panel is the window's own surface, not a card in a card.
    activity = page.evaluate("(() => { const s = getComputedStyle(document.querySelector('#wb-panel-activity'));"
                             " return {border: s.borderTopWidth, background: s.backgroundColor}; })()")
    assert activity["border"] == "0px" and activity["background"] == "rgba(0, 0, 0, 0)", activity
    # Run sections keep their reading order; on phones they stack in it.
    dom = page.evaluate("(sel) => [...document.querySelector('#wb-panel-activity').querySelectorAll(sel)]"
                        ".map(e => e.className)", RUN_SECTIONS)
    assert dom == RUN_ORDER, dom
    assert probe(page, ".ag-run-summary")["visible"]
    assert_usable(page, "#wb-pause")
    if width <= 390:
        positions = [probe(page, "." + name)["top"] for name in dom]
        assert positions == sorted(positions), positions
        # The 200% zoom equivalent of 640 and 780 px screens: every feed
        # action is reachable and Pause and Clear share a row.
        for selector in ("#close-workbench-modal", "#wb-clear", ".wb-filter-options summary"):
            assert_usable(page, selector)
        assert same_row(page, "#wb-pause", "#wb-clear")
    assert_no_sideways_scroll(page, ".workbench-modal-content")

    assert visible_panels(page) == ["activity"]
    for tab in ["changes", "commits", "prs", "activity"]:
        page.click(f'#workbench-modal [data-wb-tab="{tab}"]')
        assert visible_panels(page) == [tab], f"after clicking {tab}"
        selected = page.evaluate("[...document.querySelectorAll('#workbench-modal [data-wb-tab]')]"
                                 ".filter(b => b.getAttribute('aria-selected') === 'true').map(b => b.dataset.wbTab)")
        assert selected == [tab]
        panel = probe(page, f'#workbench-modal [data-wb-panel="{tab}"]')
        assert panel["height"] >= 200 and panel["right"] <= width + 1, (tab, panel)
    assert_no_sideways_scroll(page, ".workbench-modal-content")
    # Scrolled to the working tabs, the pinned header still masks the page
    # under it and its close button still answers.
    page.evaluate("document.querySelector('.workbench-modal-content').scrollTop = 100000")
    assert probe(page, ".workbench-modal-content>.modal-header")["background"].startswith("rgb("), "header is see-through"
    assert_usable(page, "#close-workbench-modal")


@pytest.mark.parametrize("width", [1440, 700, 390])
def test_run_summary_details_and_controls_fit(open_app, width):
    page = open_app(width)
    open_workbench(page)
    # The summary names the selected run and its state; the timeline and
    # output are filled from its recorded events.
    expect(page.locator("#ag-run-title")).to_contain_text("migrate orders")
    expect(page.locator("#ag-run-state")).to_have_text("running")
    expect(page.locator("#ag-run-timeline-list")).to_contain_text("Read schema.sql")
    expect(page.locator("#ag-run-console-output")).to_contain_text("Read schema.sql")
    for selector in (".ag-run-summary", ".ag-run-timeline", ".ag-run-console", ".ag-run-detail"):
        box = probe(page, selector)
        assert box["visible"] and box["left"] >= 0 and box["right"] <= width + 1, (selector, box)
    summary, detail, feed = probe(page, ".ag-run-summary"), probe(page, ".ag-run-detail"), probe(page, ".wb-activity")
    timeline, output = probe(page, ".ag-run-timeline"), probe(page, ".ag-run-console")
    if width >= 1280:
        assert detail["left"] >= feed["right"] + 20
        assert summary["top"] < detail["top"]
    else:
        assert detail["top"] >= output["bottom"]
        assert probe(page, ".wb-toolbar")["top"] < timeline["top"]
    if width <= 700:
        assert feed["top"] >= summary["bottom"]
    assert feed["visible"] and feed["right"] <= width + 1
    assert page.evaluate("getComputedStyle(document.querySelector('.ag-run-summary')).borderTopWidth") == "0px"
    # Empty run panels shed their height; 390 px wraps the event names.
    empty = page.evaluate(
        "(() => { document.getElementById('ag-run-console-output').textContent = '';"
        " const h = (s) => document.querySelector(s).getBoundingClientRect().height;"
        " return {timeline: h('.ag-run-timeline'), output: h('.ag-run-console')}; })()")
    assert empty["timeline"] < (200 if width <= 390 else 210) and empty["output"] < 110, empty
    if width <= 390:
        toolbar = probe(page, '.wb-panel[data-wb-panel="activity"]>.wb-toolbar')
        feed_box = probe(page, '.wb-panel[data-wb-panel="activity"]>.wb-activity')
        assert feed_box["top"] >= toolbar["bottom"] and timeline["top"] >= feed_box["bottom"], (toolbar, feed_box, timeline)
    assert_no_sideways_scroll(page, ".workbench-modal-content")
    assert_usable(page, "#close-workbench-modal")
    if width > 900:
        assert_usable(page, "#wb-dock-right")
    page.click("#close-workbench-modal")
    page.wait_for_function("document.getElementById('workbench-modal').classList.contains('hidden')")


def test_long_run_panels_remain_bounded_and_scrollable(open_app):
    page = open_app(1440)
    open_workbench(page)
    page.evaluate("""() => {
        const list = document.getElementById('ag-run-timeline-list');
        for (let i = 0; i < 40; i++) { const li = document.createElement('li'); li.textContent = 'Recorded step ' + i; list.append(li); }
        document.getElementById('ag-run-console-output').textContent = 'Output event\\n'.repeat(50);
    }""")
    for selector, cap in ((".ag-run-timeline", 260), (".ag-run-console", 160)):
        measured = page.evaluate("(sel) => { const e = document.querySelector(sel); return {height: e.getBoundingClientRect().height,"
                                 " scroll: e.scrollHeight, client: e.clientHeight}; }", selector)
        assert measured["height"] <= cap + 1 and measured["scroll"] > measured["client"], (selector, measured)


def test_workbench_tabs_follow_the_keyboard(open_app):
    page = open_app(1440)
    open_workbench(page)
    page.focus("#wb-tab-activity")
    page.keyboard.press("ArrowRight")
    assert visible_panels(page) == ["changes"]
    assert page.evaluate("document.activeElement.id") == "wb-tab-changes"
    expect(page.locator("#wb-tab-changes")).to_have_attribute("aria-selected", "true")
    assert page.evaluate("document.getElementById('wb-tab-activity').tabIndex") == -1
    expect(page.locator("#wb-changes")).to_have_attribute("aria-labelledby", "wb-tab-changes")
    page.keyboard.press("Home")
    assert visible_panels(page) == ["activity"]
    # Applying a theme identity keeps the Customize tab of the theme window.
    page.evaluate("async () => { document.querySelector('#theme-tabs [data-tab=\"theme-tab-customize\"]').click();"
                  " const m = await import('/static/js/theme.js'); m.applyThemeIdentity('dark'); }")
    assert page.evaluate("document.getElementById('theme-tab-customize').style.display") != "none"


@pytest.mark.parametrize("width", [700, 390, 320])
def test_compact_feed_filters_pause_long_title_and_status(open_app, width):
    page = open_app(width)
    open_workbench(page)
    tabs, summary, feed = probe(page, ".wb-tabs"), probe(page, ".ag-run-summary"), probe(page, ".wb-activity")
    # On phones the window opens as a bottom sheet; measure within it.
    assert tabs["height"] <= 50 and feed["top"] - probe(page, ".workbench-modal-content")["top"] < 390, (tabs, feed)
    clear = probe(page, "#wb-clear")
    assert clear["right"] <= probe(page, "#wb-panel-activity")["right"] + 1, clear
    if width == 320:
        # Feed actions belong together: a lone Clear row is hard to scan.
        assert same_row(page, "#wb-pause", "#wb-clear")
    assert summary["bottom"] < feed["top"]
    expect(page.locator("#ag-run-state")).to_have_attribute("role", "status")
    # The abbreviated PR tab keeps its full accessible name.
    expect(page.locator("#wb-tab-prs")).to_have_accessible_name(re.compile("pull requests", re.I))

    # Scope and filter fold into a disclosure.
    assert not probe(page, "#wb-scope")["visible"]
    page.focus(".wb-filter-options summary")
    page.keyboard.press("Enter")
    assert page.evaluate("document.querySelector('.wb-filter-options').open")
    assert probe(page, "#wb-scope")["visible"] and probe(page, "#wb-filter")["visible"]
    if width == 320:
        assert_no_sideways_scroll(page, ".workbench-modal-content")
        assert same_row(page, "#wb-pause", "#wb-clear")
    set_select(page, "#wb-scope", "all")
    assert page.evaluate("document.getElementById('wb-scope').value") == "all"
    page.click(".wb-filter-options summary")
    assert not probe(page, "#wb-filter")["visible"]

    pause = page.locator("#wb-pause")
    page.click("#wb-pause")
    expect(pause).to_have_class(re.compile(r"\bwb-btn-primary\b"))
    expect(page.locator("#wb-live")).to_have_attribute("data-state", "paused")
    page.click("#wb-pause")
    expect(pause).not_to_have_class(re.compile(r"\bwb-btn-primary\b"))

    page.evaluate("document.getElementById('ag-run-title').textContent = 'ExtremelyLongUnbrokenRunName'.repeat(12)")
    assert_no_sideways_scroll(page, ".workbench-modal-content")
    assert probe(page, "#ag-run-title")["right"] <= width
    page.click("#wb-tab-prs")
    assert visible_panels(page) == ["prs"]


@pytest.mark.parametrize("width", [700, 390])
def test_finished_run_feed_reconnects_and_pauses_by_keyboard(open_app, static_app, width):
    # The canned API has no event stream, so the feed keeps reconnecting
    # while the run itself stays finished. Pausing the feed never pauses
    # or changes the run.
    static_app.state.run_status = "finished"
    page = open_app(width)
    open_workbench(page)
    run_state, live, pause = page.locator("#ag-run-state"), page.locator("#wb-live"), page.locator("#wb-pause")
    expect(run_state).to_have_text("finished")
    expect(live).to_have_attribute("data-state", "reconnecting")
    expect(live).to_have_attribute("role", "status")
    assert "finished" in pause.get_attribute("aria-description")
    expect(pause).not_to_have_class(re.compile(r"\bwb-btn-primary\b"))
    pause.focus()
    page.keyboard.press("Space")
    expect(pause).to_have_class(re.compile(r"\bwb-btn-primary\b"))
    expect(live).to_have_attribute("data-state", "paused")
    # Paused, the status still says the connection is being retried.
    expect(live).to_have_text(re.compile("reconnect", re.I))
    expect(run_state).to_have_text("finished")
    page.keyboard.press("Space")
    expect(live).to_have_attribute("data-state", "reconnecting")
    expect(pause).not_to_have_class(re.compile(r"\bwb-btn-primary\b"))
    # Clearing the feed keeps the explanation that Pause is not a run pause.
    page.click("#wb-clear")
    assert pause.get_attribute("title")


@pytest.mark.parametrize("width", [1440, 700, 390])
def test_workbench_opens_on_the_branch_and_edits_its_changed_file(open_app, width):
    page = open_app(width)
    open_workbench(page, activity=False)
    page.wait_for_selector("#wb-diffpane .wb-diff")
    assert visible_panels(page) == ["changes"]
    assert page.evaluate("document.querySelector('.wb-repo-select').value") == "/repo/workbench"
    expect(page.locator("#wb-changes .wb-file")).to_have_count(2)
    assert_no_sideways_scroll(page, ".workbench-modal-content")
    # Under 720 px the diff is unified, wider it is split.
    mode = "unified" if width < 720 else "split"
    expect(page.locator("#wb-diffpane table.wb-diff")).to_have_class(re.compile(rf"\b{mode}\b"))
    assert page.evaluate("document.querySelector('#wb-diffpane td.wb-no[role=button]').tabIndex") == 0
    page.click('#wb-diffpane [data-wb-act="edit-file"]')
    page.wait_for_selector("#wb-editor-text")
    assert page.evaluate("document.activeElement.id") == "wb-editor-text"
    assert "migrate" in page.evaluate("document.getElementById('wb-editor-text').value")
    assert_usable(page, '#wb-diffpane [data-wb-act="save-file"]')
    assert_no_sideways_scroll(page, ".workbench-modal-content")
    page.click('#wb-diffpane [data-wb-act="cancel-edit"]')
    page.click('[data-wb-act="browse-files"]')
    expect(page.locator("#wb-changes .wb-file")).to_have_count(3)
    page.click('#wb-changes .wb-file[data-path="src/clean.py"]')
    page.wait_for_selector("#wb-editor-text")
    assert page.evaluate("document.querySelector('.wb-repo-select').value") == "/repo/workbench"


def test_workbench_editor_saves_the_edited_file(open_app, static_app):
    page = open_app(1440)
    open_workbench(page, activity=False)
    page.wait_for_selector("#wb-diffpane .wb-diff")
    page.click('#wb-diffpane [data-wb-act="edit-file"]')
    page.wait_for_selector("#wb-editor-text")
    status = page.locator("#wb-editor-status")
    clean = status.text_content()
    type_into_editor(page, "\n# reviewed")
    expect(status).not_to_have_text(clean)
    with page.expect_request(lambda r: r.method == "PUT" and r.url.endswith("/api/workbench/repo/file")) as saved:
        page.click('#wb-diffpane [data-wb-act="save-file"]')
    assert "# reviewed" in saved.value.post_data_json["content"]
    page.wait_for_selector("#wb-editor-text", state="detached")


def test_workbench_suggests_latest_activity_without_overriding_selection(open_app, static_app):
    static_app.state.repo_activity = (100, 200)  # the main checkout is newer
    page = open_app(1440)
    open_workbench(page, activity=False)
    page.click('[data-wb-tab="changes"]')
    page.wait_for_function("document.querySelector('.wb-repo-select')?.value === '/repo/main'")
    expect(page.locator('#wb-changes .wb-repo-select option[value="/repo/main"]')).to_have_text(re.compile("recent activity", re.I))
    set_select(page, ".wb-repo-select", "/repo/workbench")
    page.wait_for_function("document.querySelector('.wb-repo-select')?.value === '/repo/workbench'")
    assert page.evaluate("JSON.parse(localStorage.getItem('odysseus-workbench-prefs')).repo") == "/repo/workbench"


@pytest.mark.parametrize("style,width", [("classic", 1440), ("agamemnon", 700), ("agamemnon", 390)])
def test_saved_checkout_sees_newer_activity_without_forced_switch(open_app, static_app, style, width):
    page = open_app(width, style=style, workbench_prefs={"repo": "/repo/main", "tab": "changes"})
    open_workbench(page, activity=False)
    page.click('[data-wb-tab="changes"]')
    suggestion = page.locator("#wb-changes .wb-repo-suggestion")
    expect(suggestion).to_have_attribute("data-path", "/repo/workbench")
    assert page.evaluate("document.querySelector('.wb-repo-select').value") == "/repo/main"
    assert page.evaluate("JSON.parse(localStorage.getItem('odysseus-workbench-prefs')).repo") == "/repo/main"
    settle(page, "#wb-changes .wb-repo-suggestion")
    assert_usable(page, "#wb-changes .wb-repo-suggestion")
    suggestion.click()
    page.wait_for_function("document.querySelector('.wb-repo-select')?.value === '/repo/workbench'")
    assert page.evaluate("JSON.parse(localStorage.getItem('odysseus-workbench-prefs')).repo") == "/repo/workbench"
    expect(suggestion).to_have_count(0)
