"""Agamemnon Chat, Agents and Workbench as a browser actually renders them.

These run the shipped static UI in headless Chromium (stubbed API, see
tests/helpers/agamemnon_browser.py) and assert what a user gets: which
elements are drawn, whether a click at their centre reaches them, which tab
panel is showing, where focus lands, and whether anything spills sideways.
String checks on the stylesheets could not see the regressions these cover:
every Workbench tab drawn at once (a `display:block!important` beating
`.wb-panel.hidden`), a composer collapsed to 28px under the transcript, and
the Workbench close button pushed off-screen at 1440px.

Skipped when no Chromium binary is available (set ODYSSEUS_TEST_CHROMIUM).
Set AGAMEMNON_SCREENSHOT_DIR to keep a PNG of every checked state.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from tests.helpers.agamemnon_browser import (
    OTHER_ID, PARENT_ID, SESSION_ID, WORKER_ID, Chromium, StaticAppServer, chromium_path,
)

EXE = chromium_path()
pytestmark = pytest.mark.skipif(not EXE, reason="needs a Chromium binary (ODYSSEUS_TEST_CHROMIUM)")

VIEWPORTS = {1440: 920, 1280: 800, 1024: 768, 700: 844, 390: 844, 320: 740}
SHOTS = os.environ.get("AGAMEMNON_SCREENSHOT_DIR")
ODYSSEUS_THEME = json.dumps({"name": "odysseus", "colors": {
    "bg": "#211f1c", "fg": "#f2eee5", "panel": "#171614", "border": "#554b36", "red": "#c99a45"}})


@pytest.fixture(scope="module")
def server():
    with StaticAppServer() as srv:
        yield srv


@pytest.fixture(scope="module")
def browser():
    b = Chromium(EXE)
    yield b
    b.close()


@pytest.fixture
def open_app(server, browser):
    pages = []

    def _open(width: int, theme: str | None = None):
        page = browser.page(width, VIEWPORTS[width])
        if theme:
            page.before_load(f"localStorage.setItem('odysseus-theme', {json.dumps(theme)});")
        page.goto(f"{server.url}/#{SESSION_ID}", settle=0.5)
        page.wait_for("document.querySelectorAll('#chat-history .msg').length >= 4", timeout=15)
        page.wait_for("window.agentsDashboard && window.workbenchModule", timeout=10)
        time.sleep(1.0)  # past the composer's 0.3s margin transition out of the welcome state
        pages.append(page)
        return page

    yield _open
    for page in pages:
        page.close()


def shot(page, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(Path(SHOTS) / f"{name}-{page.width}.png")


def assert_usable(page, selector: str) -> dict:
    """Drawn, inside the viewport, and the top element at its centre."""
    box = page.probe(selector)
    assert box, f"{selector} missing"
    assert box["visible"], f"{selector} is not drawn: {box}"
    assert box["left"] >= -1 and box["right"] <= page.width + 1, f"{selector} off-screen horizontally: {box}"
    assert box["hit"], f"{selector} is covered by another element: {box}"
    return box


def assert_no_sideways_scroll(page, scroller: str | None = None) -> None:
    widths = page.eval(
        "(() => { const s = %s; return [document.documentElement.scrollWidth, innerWidth, s ? s.scrollWidth : 0, s ? s.clientWidth : 0]; })()"
        % (f"document.querySelector({json.dumps(scroller)})" if scroller else "null"))
    assert widths[0] <= widths[1], f"page scrolls sideways: {widths}"
    if scroller:
        assert widths[2] <= widths[3] + 1, f"{scroller} scrolls sideways: {widths}"


def visible_panels(page) -> list[str]:
    return page.eval("[...document.querySelectorAll('#workbench-modal [data-wb-panel]')]"
                     ".filter(p => getComputedStyle(p).display !== 'none' && p.getBoundingClientRect().height > 0)"
                     ".map(p => p.dataset.wbPanel)")


def open_workbench(page, *, activity=True) -> None:
    page.eval("window.workbenchModule.open()")
    page.wait_for("!document.getElementById('workbench-modal').classList.contains('hidden')")
    if activity:
        page.click('#wb-tab-activity')  # Legacy run-layout assertions explicitly inspect Activity.
    time.sleep(0.4)


def open_agents(page) -> None:
    page.eval("window.agentsDashboard.open()")
    page.wait_for("document.querySelectorAll('#agents-dashboard .ag-card').length === 3")
    time.sleep(0.3)


@pytest.mark.parametrize('width', [320, 390, 700])
def test_compact_phalanx_and_chat_strip_are_distinct_and_readable(open_app, width):
    page = open_app(width)
    assert page.eval("document.getElementById('sidebar-command-menu').hidden") is True
    page.wait_for("!document.getElementById('agent-strip').hidden")
    page.eval("document.querySelector('.agent-strip-toggle').click()")
    page.wait_for("document.querySelector('.agent-strip-toggle').getAttribute('aria-expanded') === 'false'")
    shot(page, 'strip-collapsed')
    assert_usable(page, '.agent-strip-toggle')
    assert page.probe('.agent-strip-head')['height'] < 55
    page.eval("document.querySelector('.agent-strip-toggle').click()")
    shot(page, 'strip-expanded')
    assert page.probe('.agent-strip-row')['height'] <= 130
    assert_no_sideways_scroll(page)

    open_agents(page)
    page.wait_for("document.querySelector('.ag-fleet').classList.contains('ag-fleet-compact')")
    shot(page, 'compact-phalanx')
    for card in page.eval("[...document.querySelectorAll('.ag-fleet-compact .ag-bot-card')].map(c => {const b=c.getBoundingClientRect();return {height:b.height,left:b.left,right:b.right};})"):
        assert card['height'] <= (155 if width <= 390 else 110), card
        assert card['left'] >= 0 and card['right'] <= width + 1, card
    for action in page.eval("[...document.querySelectorAll('.ag-fleet-compact .ag-card-actions button')].map(c => {const b=c.getBoundingClientRect();return {height:b.height,width:b.width,right:b.right};})"):
        assert action['height'] >= 44 and action['width'] >= 44 and action['right'] <= width + 1, action
    assert_no_sideways_scroll(page, '.agents-modal-content')


def test_chat_strip_with_half_width_docked_workbench(open_app):
    page = open_app(1024)
    open_workbench(page)
    page.click('#wb-dock-right')
    page.wait_for("document.getElementById('workbench-modal').classList.contains('modal-right-docked')")
    assert page.probe('#wb-tab-prs')['visible'] and page.probe('#wb-tab-prs')['right'] <= page.width
    page.wait_for("document.getElementById('sidebar').classList.contains('hidden')")
    shot(page, 'strip-half-workbench')
    strip, chat = page.probe('.agent-strip'), page.probe('.chat-container')
    assert strip['width'] <= chat['width'] and strip['height'] <= 155
    assert chat['width'] >= 520
    assert page.probe('#chat-history')['height'] >= 200
    assert page.probe('.chat-progress-shelf')['height'] <= 205
    assert page.eval("getComputedStyle(document.querySelector('.agent-strip-head [data-strip-act=workbench]')).display") == 'none'
    assert_usable(page, '.agent-strip-row [data-strip-act="stop"]')
    scroll = page.probe('#scroll-bottom-btn')
    if scroll['visible']:
        assert scroll['bottom'] <= page.probe('#chat-history')['bottom'], scroll
    assert_usable(page, '.agent-strip-toggle')
    assert_usable(page, '#message')
    assert_no_sideways_scroll(page)
    page.click('.agent-strip-toggle')
    page.wait_for("document.querySelector('.agent-strip-toggle').getAttribute('aria-expanded') === 'false'")
    shot(page, 'strip-half-workbench-collapsed')
    assert page.probe('.agent-strip-head')['height'] < 55
    assert_usable(page, '#message')
    assert page.probe('#chat-history')['height'] >= 250


@pytest.mark.parametrize('width', [700, 390])
def test_narrow_scribe_progress_and_workbench_do_not_cover_controls(open_app, width):
    page = open_app(width)
    page.wait_for("!document.getElementById('agent-strip').hidden")
    if width == 700:
        open_workbench(page)
        # At 700px Workbench takes the available workspace as a page;
        # docking is deliberately disabled rather than squeezing chat.
        assert not page.probe('#wb-dock-right')['visible']
        shot(page, 'combined-workbench-page')
        page.click('#close-workbench-modal')
        page.wait_for("document.getElementById('workbench-modal').classList.contains('hidden')")
    shot(page, 'combined-progress-workbench' if width == 700 else 'combined-progress-mobile')
    assert_usable(page, '.agent-strip-row [data-strip-act="stop"]')
    assert_usable(page, '#message')
    assert page.probe('.chat-progress-shelf')['height'] <= 210
    assert page.probe('#chat-history')['height'] >= 175
    assert_no_sideways_scroll(page)


# ── Chat ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("width", list(VIEWPORTS))
def test_chat_transcript_and_composer_fill_the_available_screen(open_app, width):
    page = open_app(width)
    shot(page, "chat")
    history = assert_usable(page, "#chat-history")
    bar = assert_usable(page, ".chat-input-bar")
    message = assert_usable(page, "#message")
    heading = page.probe(".ag-page-heading")

    # The composer sits under the transcript, wholly on screen, as wide as it.
    assert bar["top"] >= history["bottom"] - 1
    assert bar["bottom"] <= page.height + 1
    assert bar["width"] >= history["width"] - 2
    assert message["width"] >= bar["width"] * 0.6
    # The transcript is the part that grows, and it scrolls on its own.
    # At 1024px a live checklist may use ~80px; 300px still leaves a real
    # scrolling transcript without forcing the progress off screen.
    assert history["height"] >= (220 if width <= 390 else 300 if width == 1024 else 330)
    assert history["overflowY"] == "auto" and history["scrollHeight"] > history["clientHeight"]
    assert heading["visible"] and heading["bottom"] <= history["top"]
    assert page.probe('.ag-context-rail') is None
    assert history["width"] >= (page.width - history["left"]) * 0.88
    assert_no_sideways_scroll(page)


@pytest.mark.parametrize("width", [1440, 390])
def test_chat_composer_accepts_typing_and_transcript_scrolls(open_app, width):
    page = open_app(width)
    page.click("#message")
    assert page.eval("document.activeElement.id") == "message"
    page.type("Check the rollback plan")
    assert page.eval("document.getElementById('message').value") == "Check the rollback plan"
    before = page.eval("(() => { const h = document.getElementById('chat-history'); h.scrollTop = 0; return h.scrollTop; })()")
    after = page.eval("(() => { const h = document.getElementById('chat-history'); h.scrollTop = h.scrollHeight; return h.scrollTop; })()")
    assert before == 0 and after > 200


@pytest.mark.parametrize('width', [1440, 1024, 390, 320])
def test_expanded_checklist_and_agents_share_a_bounded_shelf(open_app, width):
    page = open_app(width)
    page.wait_for("!document.getElementById('task-checklist').hidden && !document.getElementById('agent-strip').hidden")
    assert page.eval("document.querySelector('.tc-toggle').getAttribute('aria-expanded')") == 'true'
    assert page.eval("document.querySelector('.agent-strip-toggle').getAttribute('aria-expanded')") == 'true'
    shot(page, 'progress-expanded')
    shelf = page.probe('.chat-progress-shelf')
    history, bar = page.probe('#chat-history'), page.probe('.chat-input-bar')
    assert shelf['height'] <= (250 if width <= 1024 else 205), shelf
    assert history['height'] >= (180 if width <= 390 else 260), history
    assert_usable(page, '#message')
    assert bar['top'] >= shelf['bottom'] - 1 and bar['bottom'] <= page.height + 1
    checklist, agents = page.probe('#task-checklist'), page.probe('#agent-strip')
    assert (abs(checklist['top'] - agents['top']) < 2) == (width >= 1440)
    for sel in ('.tc-list', '.agent-strip-rows'):
        box = page.probe(sel)
        assert box['height'] <= (89 if width <= 1024 else 77) and box['overflowY'] == 'auto', box
    assert_no_sideways_scroll(page)
    page.click('.tc-toggle')
    assert page.eval("document.querySelector('.tc-toggle').getAttribute('aria-expanded')") == 'false'
    page.click('.agent-strip-toggle')
    assert page.eval("document.querySelector('.agent-strip-toggle').getAttribute('aria-expanded')") == 'false'


def test_expanded_progress_with_workbench_docked_retains_chat(open_app):
    page = open_app(1440)
    page.wait_for("!document.getElementById('agent-strip').hidden")
    open_workbench(page)
    page.click('#wb-dock-right')
    page.wait_for("document.getElementById('workbench-modal').classList.contains('modal-right-docked')")
    shot(page, 'progress-workbench-docked')
    assert page.probe('.chat-progress-shelf')['height'] <= 205
    assert page.probe('#chat-history')['height'] >= 260
    assert_usable(page, '#message')
    assert page.probe('.chat-input-bar')['right'] <= page.probe('.workbench-modal-content')['left'] + 1
    assert_no_sideways_scroll(page)


def test_chat_heading_uses_the_selected_session_and_response_state(open_app):
    page = open_app(1440)
    page.wait_for("document.getElementById('ag-session-label').textContent === 'Orders migration'")
    assert page.eval("document.getElementById('model-picker-label').textContent.trim()") == 'claude-sonnet-5'
    assert page.eval("document.getElementById('ag-chat-status').textContent") == 'Ready to send'
    page.eval("window.dispatchEvent(new CustomEvent('odysseus:chat-busy-change', { detail: { active: true } }))")
    assert page.eval("document.getElementById('ag-chat-status').textContent") == 'Responding'
    page.eval("window.dispatchEvent(new CustomEvent('odysseus:chat-busy-change', { detail: { active: false } }))")
    assert page.eval("document.getElementById('ag-chat-status').textContent") == 'Ready to send'


def test_chat_send_button_reaches_the_existing_submit_flow(open_app, server):
    page = open_app(1440)
    page.click('#message')
    page.type('Review the rollout risks')
    page.click('.send-btn')
    deadline = time.time() + 8
    while not any(path == '/api/chat_stream' for path, _ in server.state.posts) and time.time() < deadline:
        time.sleep(0.1)
    assert any(path == '/api/chat_stream' for path, _ in server.state.posts), server.state.posts
    # The stub cannot produce an AI response. This verifies the real form
    # handler sends a request through the visible control, not backend success.


@pytest.mark.parametrize("width", [1440, 1024, 390])
def test_navigation_opens_the_phalanx_and_the_workbench(open_app, width):
    page = open_app(width)
    if width <= 390:
        page.eval("window._odyOpenSidebar('left')")
        page.wait_for("document.getElementById('sidebar-command-toggle').getBoundingClientRect().left >= 0")
    assert_usable(page, "#sidebar-command-toggle")
    page.click('#sidebar-command-toggle')
    page.click('#sidebar-command-menu [data-command-target="rail-agents"]')
    page.wait_for("!document.getElementById('agents-dashboard').hidden")
    assert page.probe("#agents-dashboard .ag-card")["visible"]
    page.click("#close-agents-dashboard")
    page.wait_for("document.getElementById('agents-dashboard').hidden")
    if width <= 390:
        page.eval("window._odyOpenSidebar('left')")
        page.wait_for("document.getElementById('sidebar-command-toggle').getBoundingClientRect().left >= 0")
        time.sleep(0.35)  # sidebar slide completes before coordinate click
    assert_usable(page, '#sidebar-command-toggle')
    page.click('#sidebar-command-toggle')
    page.click('#sidebar-command-menu [data-command-target="rail-workbench"]')
    page.wait_for("!document.getElementById('workbench-modal').classList.contains('hidden')")
    assert visible_panels(page) == ['changes']
    page.click('#wb-tab-activity')
    assert page.probe(".ag-run-summary")["visible"]


@pytest.mark.parametrize('width', [1440, 390])
def test_scribe_navigation_and_phalanx_identity(open_app, width):
    page = open_app(width)
    assert page.eval("document.querySelector('.ag-page-heading h1').textContent") == 'Scribe'
    assert page.eval("document.title") == 'Scribe — Agamemnon'
    assert page.eval("document.getElementById('message').placeholder") == 'Message Scribe…'
    assert page.eval("document.querySelector('link[rel=icon]').getAttribute('href')") == '/static/branding/agamemnon-trojan-helmet.svg'
    assert not page.probe('#ag-open-theme')
    assert page.eval("getComputedStyle(document.querySelector('#theme-tabs [data-tab=\"theme-tab-customize\"]')).display") != 'none'
    if width == 390:
        page.eval("window._odyOpenSidebar('left')")
        page.wait_for("document.getElementById('sidebar-command-toggle').getBoundingClientRect().left >= 0")
    assert page.probe('#sidebar-command-toggle')['visible']
    page.click('#sidebar-command-toggle')
    page.click('#sidebar-command-menu [data-command-target="rail-agents"]')
    page.wait_for("!document.getElementById('agents-dashboard').hidden")
    assert page.eval("document.querySelector('.ag-phalanx-title').textContent") == 'Phalanx'
    assert 'Command center' in page.eval("document.getElementById('ag-window-summary').textContent")
    page.click('#close-agents-dashboard')


def test_non_agamemnon_navigation_and_activity_remain_intact(open_app):
    page = open_app(1440, ODYSSEUS_THEME)
    assert page.eval("document.documentElement.dataset.style") == 'classic'
    assert page.probe('#tool-agents-btn')['visible']
    assert page.eval("document.querySelector('.ag-nav-label').textContent") == 'Phalanx'
    assert page.eval("getComputedStyle(document.querySelector('#theme-tabs [data-tab=\"theme-tab-customize\"]')).display") != 'none'
    assert page.eval("document.querySelector('link[rel=icon]').getAttribute('href')") == '/static/branding/agamemnon-trojan-helmet.svg'
    open_workbench(page, activity=False)
    assert not page.probe('.ag-run-summary')['visible']
    assert visible_panels(page) == ['changes']
    assert page.eval("document.getElementById('message').placeholder") == 'Message Scribe…'


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_theme_polish_preserves_readable_navigation_and_transcript(open_app, width):
    page = open_app(width)
    if width == 1440:
        command = assert_usable(page, '#sidebar-command-toggle')
        assert command['height'] >= 44
        assert page.probe('#sidebar-command-toggle')['fontSize'] >= 14
    # The message cards occupy the transcript rather than creating wide
    # interior gutters that squeeze code blocks at 320px.
    history, message = page.probe('#chat-history'), page.probe('#chat-history .msg-ai')
    assert message['width'] >= history['width'] * 0.85
    assert_no_sideways_scroll(page, '#chat-history')


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_workbench_activity_polish_keeps_full_tabs_and_run_content(open_app, width):
    page = open_app(width)
    open_workbench(page)
    tabs = page.eval("[...document.querySelectorAll('#workbench-modal [data-wb-tab]')].map(t => { const r=t.getBoundingClientRect(); return {height:r.height,left:r.left,right:r.right}; })")
    assert all(t['height'] >= 44 and t['left'] >= 0 and t['right'] <= width + 1 for t in tabs), tabs
    activity = page.eval("(() => { const s=getComputedStyle(document.querySelector('#wb-panel-activity')); return {border:s.borderTopWidth,background:s.backgroundColor}; })()")
    assert activity['border'] == '0px' and activity['background'] == 'rgba(0, 0, 0, 0)', activity
    assert page.probe('.ag-run-summary')['visible']
    assert_no_sideways_scroll(page, '.workbench-modal-content')
    if width <= 390:
        assert abs(tabs[0]['left'] - tabs[2]['left']) <= 1, tabs
        assert tabs[2]['right'] <= width


@pytest.mark.parametrize('width', [390, 320])
def test_phalanx_mobile_names_and_actions_are_reachable(open_app, width):
    page = open_app(width)
    open_agents(page)
    card = page.probe('#agents-dashboard .ag-card')
    name = page.probe('#agents-dashboard .ag-card .ag-card-select')
    actions = page.eval("[...document.querySelectorAll('#agents-dashboard .ag-card:first-child .ag-card-actions .wb-icon-btn')].map(b => {const r=b.getBoundingClientRect();return {width:r.width,height:r.height,left:r.left,right:r.right,top:r.top};})")
    assert name['right'] <= card['right'] and name['width'] >= 70
    assert all(a['width'] >= 44 and a['height'] >= 44 and a['right'] <= width for a in actions), actions
    assert actions and actions[0]['top'] >= card['top']
    assert_no_sideways_scroll(page, '.agents-modal-content')


@pytest.mark.parametrize('width', [1440, 390, 320])
def test_code_actions_leave_the_code_readable(open_app, width):
    page = open_app(width)
    for flip in (False, True):
        code = page.eval("""(() => {const p=document.querySelector('#chat-history pre:has(> .copy-code)');
            if (%s) p.querySelectorAll('.copy-code,.edit-code,.run-code').forEach(b => b.classList.add('bottom'));
            const content=p.querySelector('code'); const actions=[...p.querySelectorAll('.copy-code,.edit-code,.run-code')];
            return {padding:parseFloat(getComputedStyle(p).paddingTop),
                    buttonBottom:Math.max(...actions.map(b=>b.getBoundingClientRect().bottom)),
                    codeTop:content.getBoundingClientRect().top};})()""" % json.dumps(flip))
        assert code['padding'] >= 44 and code['codeTop'] >= code['buttonBottom'], code


@pytest.mark.parametrize('width', [1440, 1024, 390, 320])
def test_workbench_sheds_empty_run_panel_height(open_app, width):
    page = open_app(width)
    open_workbench(page)
    timeline, output = page.probe('.ag-run-timeline'), page.probe('.ag-run-console')
    # 320px wraps the event names; let real content grow instead of clipping.
    assert timeline['height'] < (200 if width <= 390 else 210) and output['height'] < 110, (timeline, output)
    assert_no_sideways_scroll(page, '.workbench-modal-content')
    if width <= 390:
        toolbar, feed, detail = (page.probe(s) for s in ('.wb-panel[data-wb-panel="activity"]>.wb-toolbar', '.wb-panel[data-wb-panel="activity"]>.wb-activity', '.ag-run-detail'))
        assert toolbar['top'] >= output['bottom'] and feed['top'] >= toolbar['bottom'], (toolbar, feed)
        assert detail['top'] >= feed['bottom'], (feed, detail)


def test_long_workbench_run_panels_remain_bounded_and_scrollable(open_app):
    page = open_app(1440)
    open_workbench(page)
    page.eval("""(() => {
        const list=document.getElementById('ag-run-timeline-list');
        for (let i=0;i<40;i++) { const li=document.createElement('li');li.textContent='Recorded step '+i;list.append(li); }
        document.getElementById('ag-run-console-output').textContent = ('Output event\\n').repeat(50);
    })()""")
    for selector, cap in (('.ag-run-timeline', 260), ('.ag-run-console', 160)):
        measured = page.eval("(() => {const e=document.querySelector('%s');return {height:e.getBoundingClientRect().height,scroll:e.scrollHeight,client:e.clientHeight};})()" % selector)
        assert measured['height'] <= cap + 1 and measured['scroll'] > measured['client'], measured


def test_workbench_keyboard_tabs_and_theme_switch(open_app):
    page = open_app(1440)
    open_workbench(page)
    page.eval("document.getElementById('wb-tab-activity').focus(); document.getElementById('wb-tab-activity').dispatchEvent(new KeyboardEvent('keydown', {key:'ArrowRight',bubbles:true}))")
    assert visible_panels(page) == ['changes']
    assert page.eval("document.activeElement.id") == 'wb-tab-changes'
    assert page.eval("document.getElementById('wb-tab-changes').getAttribute('aria-selected')") == 'true'
    assert page.eval("document.getElementById('wb-tab-activity').tabIndex") == -1
    assert page.eval("document.getElementById('wb-changes').getAttribute('aria-labelledby')") == 'wb-tab-changes'
    page.eval("document.getElementById('wb-tab-changes').dispatchEvent(new KeyboardEvent('keydown', {key:'Home',bubbles:true}))")
    assert visible_panels(page) == ['activity']
    page.eval("document.querySelector('#theme-tabs [data-tab=\"theme-tab-customize\"]').click(); import('/static/js/theme.js').then(m => m.applyThemeIdentity('dark'))")
    assert page.eval("document.getElementById('theme-tab-customize').style.display") != 'none'


# ── Workbench ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("width", list(VIEWPORTS))
def test_workbench_draws_only_the_selected_tab(open_app, width):
    page = open_app(width)
    open_workbench(page)
    shot(page, "workbench")
    if width <= 390:
        tabs = page.eval("[...document.querySelectorAll('#workbench-modal [data-wb-tab]')].map(t => ({label:t.textContent.trim(), right:t.getBoundingClientRect().right, visible:getComputedStyle(t).display !== 'none'}))")
        assert all(tab['visible'] and tab['right'] <= width + 1 for tab in tabs), tabs
        assert any('Pull Requests' in tab['label'] for tab in tabs), tabs
    assert visible_panels(page) == ["activity"]
    for tab, label in (("changes", "Changes"), ("commits", "Commits"), ("prs", "Pull Requests"), ("activity", "Activity")):
        selector = f'#workbench-modal [data-wb-tab="{tab}"]'
        page.click(selector)
        assert visible_panels(page) == [tab], f"after clicking {label}"
        assert page.eval(f"document.querySelector('{selector}').getAttribute('aria-selected')") == "true"
        assert page.eval("[...document.querySelectorAll('#workbench-modal [data-wb-tab]')]"
                         ".filter(b => b.getAttribute('aria-selected') === 'true').length") == 1
        panel = page.probe(f'#workbench-modal [data-wb-panel="{tab}"]')
        assert panel["height"] >= 200 and panel["right"] <= width + 1
    assert_no_sideways_scroll(page, ".workbench-modal-content")
    # Scrolled to the working tabs, the pinned header still masks the page
    # under it and its close button still answers.
    page.eval("document.querySelector('.workbench-modal-content').scrollTop = 100000")
    assert page.probe(".workbench-modal-content>.modal-header")["background"] == "rgb(17, 20, 23)"
    assert_usable(page, "#close-workbench-modal")


@pytest.mark.parametrize("width", list(VIEWPORTS))
def test_workbench_summary_details_and_controls_fit(open_app, width):
    page = open_app(width)
    open_workbench(page)
    for selector in (".ag-run-summary", ".ag-run-timeline", ".ag-run-console", ".ag-run-detail"):
        box = page.probe(selector)
        assert box["visible"] and box["left"] >= 0 and box["right"] <= width + 1, (selector, box)
    summary, detail = page.probe(".ag-run-summary"), page.probe(".ag-run-detail")
    if width >= 1280:
        assert detail["left"] >= summary["right"] + 20 and abs(detail["top"] - summary["top"]) <= 2
    else:
        assert detail["top"] >= page.probe(".ag-run-console")["bottom"]
    # The run timeline and output are filled from recorded events.
    page.wait_for("document.getElementById('ag-run-timeline-list').textContent.includes('Read schema.sql')")
    assert "Read schema.sql" in page.eval("document.getElementById('ag-run-console-output').textContent")
    assert_usable(page, "#close-workbench-modal")
    if width > 900:
        assert_usable(page, "#wb-dock-right")
    page.click("#close-workbench-modal")
    page.wait_for("document.getElementById('workbench-modal').classList.contains('hidden')")


def test_workbench_docks_beside_the_chat_and_keeps_working_tabs(open_app):
    page = open_app(1440)
    open_workbench(page)
    page.click("#wb-dock-right")
    page.wait_for("document.getElementById('workbench-modal').classList.contains('modal-right-docked')")
    time.sleep(0.4)
    shot(page, "workbench-docked")
    content = page.probe(".workbench-modal-content")
    assert page.eval("document.getElementById('message').placeholder") == 'Message Scribe…'
    assert content["left"] > 600 and content["right"] <= 1441
    for selector in ('.ag-run-summary', '.ag-run-timeline', '.ag-run-console', '.ag-run-detail'):
        box = page.probe(selector)
        assert box['visible'] and box['right'] <= 1440, (selector, box)
    assert page.probe('.ag-run-detail')['top'] >= page.probe('.ag-run-console')['bottom']
    # The chat makes room and stays usable beside it.
    for selector in ("#chat-history", ".chat-input-bar"):
        assert page.probe(selector)["right"] <= content["left"] + 1, selector
    assert_usable(page, "#message")
    page.click('#workbench-modal [data-wb-tab="commits"]')
    assert visible_panels(page) == ["commits"]
    assert_usable(page, "#close-workbench-modal")
    page.click("#close-workbench-modal")
    page.wait_for("document.getElementById('workbench-modal').classList.contains('hidden')")
    assert not page.eval("document.body.classList.contains('right-dock-active')")


# ── Agents ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("width", list(VIEWPORTS))
def test_agents_cards_form_a_two_column_board_with_reachable_detail(open_app, width):
    page = open_app(width)
    open_agents(page)
    shot(page, "agents")
    cards = page.eval("[...document.querySelectorAll('#agents-dashboard .ag-card')].map(c => {"
                      " const r = c.getBoundingClientRect(); return {sid: c.dataset.sid, left: r.left, right: r.right, top: r.top}; })")
    columns = {round(c["left"]) for c in cards}
    # Two columns where a card keeps 360px (the 1280+ boards), else one.
    assert len(columns) == (2 if width >= 1280 else 1), cards
    assert all(c["right"] <= width + 1 for c in cards)
    assert_usable(page, "#close-agents-dashboard")
    if width > 900:
        assert_usable(page, "#ag-dock-right")
        assert_usable(page, "#ag-dock-left")
    assert_no_sideways_scroll(page, ".agents-modal-content")

    # Selecting a unit by its name moves the detail into view with focus on it.
    page.click(f'.ag-card-select[data-sid="{OTHER_ID}"]')
    page.wait_for(f"document.querySelector('#ag-detail').dataset.sessionId === '{OTHER_ID}'")
    time.sleep(0.6)  # smooth scroll
    assert page.eval("document.activeElement.classList.contains('ag-detail-name')")
    assert page.eval("document.activeElement.textContent") == "Release reviewer"
    assert page.eval(f"document.querySelector('.ag-card[data-sid=\"{OTHER_ID}\"]').classList.contains('active')")
    name = page.probe(".ag-detail-name")
    assert 0 <= name["top"] < page.height and name["visible"]
    # The detail tabs are real tabs.
    page.click("#ag-tab-steering")
    assert page.eval("document.getElementById('ag-tab-steering').getAttribute('aria-selected')") == "true"
    assert_usable(page, "#ag-reply")


def test_launch_worker_form_opens_where_the_button_is(open_app):
    page = open_app(1440)
    open_agents(page)
    page.click('[data-ag="launch"]')
    page.wait_for("document.getElementById('ag-launch')")
    time.sleep(0.3)
    launch, fleet = page.probe("#ag-launch"), page.probe(".ag-fleet")
    assert launch["visible"] and launch["top"] < fleet["top"]
    assert 0 <= launch["top"] < page.height
    assert page.eval("document.activeElement.id") == "ag-task"


@pytest.mark.parametrize("width", [1440, 390])
def test_parent_soldier_appearance_saves_and_keeps_focus(open_app, server, width):
    page = open_app(width)
    open_agents(page)
    page.click(f'.ag-card-select[data-sid="{PARENT_ID}"]')
    page.wait_for(f"document.querySelector('#ag-detail').dataset.sessionId === '{PARENT_ID}'")
    option = assert_usable(page, '.ag-appearance-option:has(#ag-appearance-scout)')
    assert option["width"] >= 100 and option["height"] >= 100
    page.click('.ag-appearance-option:has(#ag-appearance-scout)')
    page.wait_for("document.getElementById('ag-appearance-status').textContent === 'Appearance saved'")
    assert server.state.appearance.get(PARENT_ID) == "scout"
    assert page.eval("document.activeElement.id") == "ag-appearance-scout"
    assert page.eval("document.getElementById('ag-appearance-scout').checked")
    assert page.eval(f"document.querySelector('.ag-card[data-sid=\"{PARENT_ID}\"] .ag-bot').dataset.soldierVariant") == "scout"
    # Arrow keys move through the choices without the poll stealing focus.
    page.press("ArrowRight", key_code=39)
    deadline = time.time() + 5
    while server.state.appearance.get(PARENT_ID) != "reviewer" and time.time() < deadline:
        time.sleep(0.1)
    assert server.state.appearance.get(PARENT_ID) == "reviewer"
    page.wait_for("document.getElementById('ag-appearance-status').textContent === 'Appearance saved'")
    assert page.eval("document.activeElement.id") == "ag-appearance-reviewer"
    server.state.appearance.clear()

    # A worker is not a parent: it has no picker.
    page.click(f'.ag-card-select[data-sid="{WORKER_ID}"]')
    page.wait_for(f"document.querySelector('#ag-detail').dataset.sessionId === '{WORKER_ID}'")
    assert page.eval("document.querySelector('#ag-detail .ag-appearance')") is None


def test_agents_dock_and_close_keep_working(open_app):
    page = open_app(1440)
    open_agents(page)
    page.click("#ag-dock-right")
    page.wait_for("document.getElementById('agents-dashboard').classList.contains('modal-right-docked')")
    time.sleep(0.4)
    shot(page, "agents-docked")
    content = page.probe(".agents-modal-content")
    assert page.eval("document.getElementById('message').placeholder") == 'Message Scribe…'
    assert content["left"] > 500 and content["right"] <= 1441
    for selector in ("#chat-history", ".chat-input-bar"):
        assert page.probe(selector)["right"] <= content["left"] + 1, selector
    assert_usable(page, "#message")
    assert_usable(page, "#close-agents-dashboard")
    page.click("#close-agents-dashboard")
    page.wait_for("document.getElementById('agents-dashboard').hidden")


# ── Text and theme ──────────────────────────────────────────────────────────

def test_agamemnon_text_is_legible(open_app):
    page = open_app(1440)
    checks = [".ag-page-heading p", ".ag-page-heading strong", "#message"]
    for selector in checks:
        assert page.contrast(selector) >= 4.5, selector
        assert page.probe(selector)["fontSize"] >= 12, selector
    open_agents(page)
    for selector in (".ag-row-latest", ".ag-row-meta-inline", ".ag-window-title>span", ".ag-appearance p"):
        assert page.contrast(selector) >= 4.5, selector
        assert page.probe(selector)["fontSize"] >= 12, selector
    page.click("#close-agents-dashboard")
    open_workbench(page)
    for selector in (".ag-run-summary p", ".ag-run-detail dd", ".ag-run-detail p", ".ag-run-console-output"):
        assert page.contrast(selector) >= 4.5, selector
        assert page.probe(selector)["fontSize"] >= 12, selector


@pytest.mark.parametrize("width", [1440, 390])
def test_odysseus_theme_keeps_its_own_layout(open_app, width):
    page = open_app(width, theme=ODYSSEUS_THEME)
    assert page.eval("document.documentElement.dataset.theme") == "odysseus"
    shot(page, "odysseus-chat")
    for selector in (".ag-page-heading", ".ag-context-rail"):
        assert not (page.probe(selector) or {}).get("visible"), selector
    bar = assert_usable(page, ".chat-input-bar")
    assert_usable(page, "#message")
    assert bar["width"] <= 802  # the base composer keeps its 800px measure
    open_workbench(page, activity=False)
    assert not page.probe(".ag-run-control")["visible"]
    assert visible_panels(page) == ["changes"]
    page.click('#workbench-modal [data-wb-tab="commits"]')
    assert visible_panels(page) == ["commits"]
    page.click('#workbench-modal [data-wb-tab="changes"]')
    assert visible_panels(page) == ["changes"]
    assert_no_sideways_scroll(page)


@pytest.mark.parametrize('width', [1440, 700, 390])
def test_workbench_defaults_to_branch_and_can_edit_its_changed_file(open_app, width):
    page = open_app(width)
    open_workbench(page, activity=False)
    page.wait_for("document.querySelector('#wb-diffpane .wb-diff')")
    assert visible_panels(page) == ['changes']
    assert page.eval("document.querySelector('.wb-repo-select').value") == '/repo/workbench'
    assert page.eval("document.querySelectorAll('#wb-changes .wb-file').length") == 2
    shot(page, 'branch-review')
    assert_no_sideways_scroll(page, '.workbench-modal-content')
    mode = 'unified' if width < 720 else 'split'
    assert page.eval("document.querySelector('#wb-diffpane table.wb-diff').className.includes(%s)" % json.dumps(mode)), mode
    assert page.eval("document.querySelector('#wb-diffpane td.wb-no[role=button]').tabIndex") == 0
    page.click('#wb-diffpane [data-wb-act="edit-file"]')
    page.wait_for("document.getElementById('wb-editor-text')")
    assert page.eval("document.activeElement.id") == 'wb-editor-text'
    assert 'migrate' in page.eval("document.getElementById('wb-editor-text').value")
    shot(page, 'branch-editor')
    assert_usable(page, '#wb-diffpane [data-wb-act="save-file"]')
    assert_no_sideways_scroll(page, '.workbench-modal-content')
    page.click('#wb-diffpane [data-wb-act="cancel-edit"]')
    page.click('[data-wb-act="browse-files"]')
    page.wait_for("document.querySelectorAll('#wb-changes .wb-file').length === 3")
    assert page.eval("document.querySelector('#wb-changes').textContent.includes('src/clean.py')")
    page.click('#wb-changes .wb-file[data-path="src/clean.py"]')
    page.wait_for("document.getElementById('wb-editor-text')")
    assert page.eval("document.querySelector('.wb-repo-select').value") == '/repo/workbench'


def test_workbench_editor_save_and_review_flow(open_app, server):
    page = open_app(1440)
    open_workbench(page, activity=False)
    page.wait_for("document.querySelector('#wb-diffpane .wb-diff')")
    page.click('#wb-diffpane [data-wb-act="edit-file"]')
    page.wait_for("document.getElementById('wb-editor-text')")
    page.eval("(() => {const input=document.getElementById('wb-editor-text'); input.value += '\\n# reviewed'; input.dispatchEvent(new Event('input',{bubbles:true}));})()")
    assert page.eval("document.getElementById('wb-editor-status').textContent") == 'Unsaved changes'
    page.click('#wb-diffpane [data-wb-act="save-file"]')
    page.wait_for("!document.getElementById('wb-editor-text')")
    assert any(path == '/api/workbench/repo/file' and '# reviewed' in body.get('content', '')
               for path, body in server.state.posts)


@pytest.mark.parametrize('theme', [None, ODYSSEUS_THEME])
def test_navigation_and_background_effect_survive_customization(open_app, theme):
    page = open_app(1440, theme)
    style = page.eval('document.documentElement.dataset.style')
    assert page.probe('.sidebar-brand-icon')['width'] == 40
    assert page.probe('.sidebar')['width'] == 240
    hamburger, logo = page.probe('#hamburger-btn'), page.probe('.sidebar-brand-icon')
    assert logo['left'] - hamburger['right'] >= 8, (hamburger, logo)
    assert page.probe('#sidebar-command-toggle')['visible'] == (style == 'agamemnon')
    assert page.probe('#sidebar-new-chat-btn')['visible'] == (style == 'classic')
    page.eval("document.getElementById('theme-modal').classList.remove('hidden')")
    page.click('#theme-tabs [data-tab="theme-tab-customize"]')
    assert page.eval("(() => { const c=document.querySelector('#theme-modal .admin-card:has(#theme-font-select)'); return c.scrollWidth <= c.clientWidth + 1 })()")
    page.eval("(() => {const sel=document.getElementById('theme-bg-pattern-select'); sel.value='dots'; sel.dispatchEvent(new Event('change',{bubbles:true}));})()")
    assert 'bg-pattern-dots' in page.eval('document.body.className')
    assert 'radial-gradient' in page.eval('getComputedStyle(document.body).backgroundImage')
    shot(page, 'customization-' + style)
