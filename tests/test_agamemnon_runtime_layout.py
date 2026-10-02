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

VIEWPORTS = {1440: 920, 1280: 800, 1024: 768, 390: 844, 320: 740}
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


def open_workbench(page) -> None:
    page.eval("window.workbenchModule.open()")
    page.wait_for("!document.getElementById('workbench-modal').classList.contains('hidden')")
    time.sleep(0.4)


def open_agents(page) -> None:
    page.eval("window.agentsDashboard.open()")
    page.wait_for("document.querySelectorAll('#agents-dashboard .ag-card').length === 3")
    time.sleep(0.3)


# ── Chat ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("width", list(VIEWPORTS))
def test_chat_transcript_composer_and_context_share_the_screen(open_app, width):
    page = open_app(width)
    shot(page, "chat")
    history = assert_usable(page, "#chat-history")
    bar = assert_usable(page, ".chat-input-bar")
    message = assert_usable(page, "#message")
    heading = page.probe(".ag-page-heading")
    rail = page.probe(".ag-context-rail")

    # The composer sits under the transcript, wholly on screen, as wide as it.
    assert bar["top"] >= history["bottom"] - 1
    assert bar["bottom"] <= page.height + 1
    assert bar["width"] >= history["width"] - 2
    assert message["width"] >= bar["width"] * 0.6
    # The transcript is the part that grows, and it scrolls on its own.
    assert history["height"] >= (220 if width <= 390 else 330)
    assert history["overflowY"] == "auto" and history["scrollHeight"] > history["clientHeight"]
    assert heading["visible"] and heading["bottom"] <= history["top"]
    assert rail["visible"]
    if width >= 1280:
        # The context column stands beside the transcript, not on it.
        assert rail["left"] >= history["right"] + 20
        assert rail["right"] <= width
        assert abs(rail["top"] - history["top"]) <= 2
    else:
        # A strip between heading and transcript, never wider than the screen.
        assert heading["bottom"] <= rail["top"] and rail["bottom"] <= history["top"]
        assert rail["right"] <= width
    assert_no_sideways_scroll(page)
    assert_no_sideways_scroll(page, ".ag-context-rail")


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


def test_chat_context_uses_the_selected_session_and_response_state(open_app):
    page = open_app(1440)
    page.wait_for("document.getElementById('ag-session-label').textContent === 'Orders migration'")
    assert page.eval("document.getElementById('ag-chat-model').textContent") == 'claude-sonnet-5'
    assert page.eval("document.getElementById('ag-chat-status').textContent") == 'Ready to send'
    page.eval("window.dispatchEvent(new CustomEvent('odysseus:chat-busy-change', { detail: { active: true } }))")
    assert page.eval("document.getElementById('ag-chat-status').textContent") == 'Responding'
    assert page.eval("document.getElementById('ag-context-status').textContent") == 'Responding'
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
def test_context_actions_open_the_phalanx_and_the_workbench(open_app, width):
    page = open_app(width)
    assert_usable(page, "#ag-open-agents")
    page.click("#ag-open-agents")
    page.wait_for("!document.getElementById('agents-dashboard').hidden")
    assert page.probe("#agents-dashboard .ag-card")["visible"]
    page.click("#close-agents-dashboard")
    page.wait_for("document.getElementById('agents-dashboard').hidden")
    page.click("#ag-open-workbench")
    page.wait_for("!document.getElementById('workbench-modal').classList.contains('hidden')")
    assert page.probe(".ag-run-summary")["visible"]


@pytest.mark.parametrize('width', [1440, 390])
def test_scribe_navigation_and_phalanx_identity(open_app, width):
    page = open_app(width)
    assert page.eval("document.querySelector('.ag-page-heading h1').textContent") == 'Scribe'
    assert page.eval("document.title") == 'Scribe — Agamemnon'
    assert page.eval("document.getElementById('message').placeholder") == 'Message Scribe…'
    assert page.eval("document.querySelector('link[rel=icon]').getAttribute('href')") == '/static/branding/agamemnon-trojan-helmet.svg'
    assert not page.probe('#ag-open-theme')
    assert not page.probe('#theme-tabs [data-tab="theme-tab-customize"]')['visible']
    if width == 390:
        page.eval("window._odyOpenSidebar('left')")
        page.wait_for("document.getElementById('sidebar-agents-shortcut').getBoundingClientRect().left >= 0")
    assert page.probe('#sidebar-agents-shortcut')['visible']
    page.click('#sidebar-agents-shortcut')
    page.wait_for("!document.getElementById('agents-dashboard').hidden")
    assert page.eval("document.querySelector('.ag-phalanx-title').textContent") == 'Phalanx'
    assert 'Command center' in page.eval("document.getElementById('ag-window-summary').textContent")
    page.click('#close-agents-dashboard')


def test_non_agamemnon_navigation_and_activity_remain_intact(open_app):
    page = open_app(1440, ODYSSEUS_THEME)
    assert not page.probe('.ag-command-link')['visible']
    assert page.probe('#tool-agents-btn')['visible']
    assert page.eval("document.querySelector('.ody-nav-label').textContent") == 'Agents'
    assert page.eval("getComputedStyle(document.querySelector('#theme-tabs [data-tab=\"theme-tab-customize\"]')).display") != 'none'
    assert page.eval("document.querySelector('link[rel=icon]').getAttribute('href')") != '/static/branding/agamemnon-trojan-helmet.svg'
    open_workbench(page)
    assert not page.probe('.ag-run-summary')['visible']
    assert visible_panels(page) == ['activity']
    assert page.eval("document.getElementById('message').placeholder") == 'Message Odysseus...'


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
    assert page.eval("document.getElementById('theme-tab-customize').style.display") == 'none'


# ── Workbench ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("width", list(VIEWPORTS))
def test_workbench_draws_only_the_selected_tab(open_app, width):
    page = open_app(width)
    open_workbench(page)
    shot(page, "workbench")
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
    assert content["left"] > 600 and content["right"] <= 1441
    # The chat makes room and stays usable beside it.
    for selector in ("#chat-history", ".chat-input-bar", ".ag-context-rail"):
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
    assert content["left"] > 500 and content["right"] <= 1441
    for selector in ("#chat-history", ".chat-input-bar", ".ag-context-rail"):
        assert page.probe(selector)["right"] <= content["left"] + 1, selector
    assert_usable(page, "#message")
    assert_usable(page, "#close-agents-dashboard")
    page.click("#close-agents-dashboard")
    page.wait_for("document.getElementById('agents-dashboard').hidden")


# ── Text and theme ──────────────────────────────────────────────────────────

def test_agamemnon_text_is_legible(open_app):
    page = open_app(1440)
    checks = [".ag-page-heading p", ".ag-context-rail dd", ".ag-context-rail section>p", "#current-meta", "#message"]
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
        assert not page.probe(selector)["visible"], selector
    bar = assert_usable(page, ".chat-input-bar")
    assert_usable(page, "#message")
    assert bar["width"] <= 802  # the base composer keeps its 800px measure
    open_workbench(page)
    assert not page.probe(".ag-run-control")["visible"]
    assert visible_panels(page) == ["activity"]
    page.click('#workbench-modal [data-wb-tab="changes"]')
    assert visible_panels(page) == ["changes"]
    assert_no_sideways_scroll(page)
