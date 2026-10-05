"""The Phalanx (static/js/agentsDashboard.js): the agents fleet, its detail
pane and the soldier appearance picker, floating, docked and on phones.

The fixture fleet is a running lead with one worker under it and a finished
reviewer. 320 px stays where a test checks phone touch targets and the
classic toolbar wrapping.
"""
from __future__ import annotations

import re

import pytest

from tests.helpers.static_app import (
    LIGHT_THEME, ODYSSEUS_THEME, OTHER_ID, PARENT_ID, WORKER_ID, assert_no_sideways_scroll, assert_usable, contrast,
    expect, open_agents, probe, select_agent, set_checkbox, settle, wait_ready,
)

pytestmark = pytest.mark.browser

LEAD_CARD = f'.ag-card[data-sid="{PARENT_ID}"]'


def boxes(page, selector: str) -> list[dict]:
    return page.evaluate("(sel) => [...document.querySelectorAll(sel)].filter(e => e.offsetParent).map(e => {"
                         " const r = e.getBoundingClientRect(); return {left: r.left, right: r.right, top: r.top,"
                         " bottom: r.bottom, width: r.width, height: r.height}; })", selector)


def lead_variant(page):
    return expect(page.locator(f"{LEAD_CARD} .ag-bot").first)


@pytest.mark.parametrize("width", [1440, 700, 390])
def test_fleet_stays_inside_its_window_and_selecting_opens_the_detail(open_app, width):
    page = open_app(width)
    open_agents(page)
    window = probe(page, ".agents-modal-content")
    cards = boxes(page, "#agents-dashboard .ag-card")
    # Nested worker cards are indented inside the shared fleet/detail split.
    assert all(c["left"] >= window["left"] for c in cards), cards
    assert all(c["right"] <= width + 1 for c in cards), cards
    assert_usable(page, "#close-agents-dashboard")
    if width > 900:
        assert_usable(page, "#ag-dock-right")
        assert_usable(page, "#ag-dock-left")
    assert_no_sideways_scroll(page, ".agents-modal-content")

    # Selecting a unit by its name updates the detail without replacing
    # the window, and moves focus to the detail's name.
    select_agent(page, OTHER_ID)
    settle(page, ".ag-detail-name")
    assert page.evaluate("document.activeElement.classList.contains('ag-detail-name')")
    assert page.evaluate("document.activeElement.textContent") == "Release reviewer"
    expect(page.locator(f'.ag-card[data-sid="{OTHER_ID}"]')).to_have_class(re.compile(r"\bactive\b"))
    name = probe(page, ".ag-detail-name")
    assert name["visible"] and 0 <= name["top"] < page.viewport_size["height"], name
    # The detail tabs are real tabs.
    page.click("#ag-tab-steering")
    expect(page.locator("#ag-tab-steering")).to_have_attribute("aria-selected", "true")
    page.locator("#ag-reply").scroll_into_view_if_needed()
    settle(page, "#ag-reply")
    assert_usable(page, "#ag-reply")


def test_launch_form_opens_over_the_fleet_with_focus_in_the_task(open_app):
    page = open_app(1440)
    open_agents(page)
    page.click('[data-ag="launch"]')
    page.wait_for_selector("#ag-launch")
    settle(page, "#ag-launch")
    launch, fleet = probe(page, "#ag-launch"), probe(page, ".ag-fleet")
    # The floating window opens the form over the fleet pane; page mode used
    # to put it in the document flow above the fleet.
    assert launch["visible"] and launch["top"] >= fleet["top"]
    assert 0 <= launch["top"] < page.viewport_size["height"]
    assert page.evaluate("document.activeElement.id") == "ag-task"


@pytest.mark.parametrize("width", [1440, 390])
def test_lead_soldier_appearance_saves_and_keeps_focus(open_app, static_app, width):
    page = open_app(width)
    open_agents(page)
    select_agent(page, PARENT_ID)
    scout = ".ag-appearance-option:has(#ag-appearance-scout)"
    page.locator(scout).scroll_into_view_if_needed()
    settle(page, scout)
    option = assert_usable(page, scout)
    assert option["width"] >= 100 and option["height"] >= 100, option
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith(f"/{PARENT_ID}/appearance")):
        page.click(scout)
    lead_variant(page).to_have_attribute("data-soldier-variant", "scout")
    assert static_app.state.appearance.get(PARENT_ID) == "scout"
    expect(page.locator("#ag-appearance-status")).not_to_have_text("")
    assert page.evaluate("document.activeElement.id") == "ag-appearance-scout"
    assert page.evaluate("document.getElementById('ag-appearance-scout').checked")
    # Arrow keys move through the choices, and the fleet poll does not take
    # focus away.
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith(f"/{PARENT_ID}/appearance")):
        page.keyboard.press("ArrowRight")
    lead_variant(page).to_have_attribute("data-soldier-variant", "reviewer")
    assert static_app.state.appearance.get(PARENT_ID) == "reviewer"
    assert page.evaluate("document.activeElement.id") == "ag-appearance-reviewer"
    # A worker is not a lead: it has no picker.
    select_agent(page, WORKER_ID)
    expect(page.locator("#ag-detail .ag-appearance")).to_have_count(0)


def test_docked_phalanx_leaves_the_chat_usable_and_closes(open_app):
    page = open_app(1440)
    open_agents(page)
    page.click("#ag-dock-right")
    page.wait_for_function("document.getElementById('agents-dashboard').classList.contains('modal-right-docked')")
    settle(page, ".agents-modal-content")
    content = probe(page, ".agents-modal-content")
    assert content["left"] > 500 and content["right"] <= 1441, content
    for selector in ("#chat-history", ".chat-input-bar"):
        assert probe(page, selector)["right"] <= content["left"] + 1, selector
    assert_usable(page, "#message")
    assert_usable(page, "#close-agents-dashboard")
    page.click("#close-agents-dashboard")
    page.wait_for_function("document.getElementById('agents-dashboard').hidden")


def test_narrow_docked_phalanx_keeps_names_and_actions(open_app):
    page = open_app(1440)
    open_agents(page)
    page.click("#ag-dock-right")
    page.wait_for_function("document.getElementById('agents-dashboard').classList.contains('modal-right-docked')")
    settle(page, ".agents-modal-content")
    lead = page.evaluate("""(sel) => { const c = document.querySelector(sel);
      const name = c.querySelector('.ag-row-name'), action = c.querySelector('.ag-card-actions');
      const n = name.getBoundingClientRect(), a = action.getBoundingClientRect();
      return {nameWidth: n.width, nameScroll: name.scrollWidth, nameBottom: n.bottom, actionTop: a.top,
              actions: [...action.children].map(b => ({width: b.getBoundingClientRect().width,
                                                       height: b.getBoundingClientRect().height}))}; }""", LEAD_CARD)
    assert lead["nameWidth"] >= 100 and lead["nameScroll"] <= lead["nameWidth"] + 2, lead
    assert lead["actionTop"] >= lead["nameBottom"] - 1, lead
    assert all(a["width"] >= 44 and a["height"] >= 40 for a in lead["actions"]), lead


@pytest.mark.parametrize("width", [390, 320])
def test_phone_fleet_names_actions_and_metadata_are_reachable_and_readable(open_app, width):
    page = open_app(width)
    open_agents(page)
    card = probe(page, "#agents-dashboard .ag-card")
    name = probe(page, "#agents-dashboard .ag-card .ag-card-select")
    actions = boxes(page, "#agents-dashboard .ag-card:first-child .ag-card-actions .wb-icon-btn")
    assert name["right"] <= card["right"] and name["width"] >= 70, (name, card)
    assert actions and actions[0]["top"] >= card["top"], actions
    assert all(a["width"] >= 44 and a["height"] >= 44 and a["right"] <= width for a in actions), actions
    compact_card, compact_actions = probe(page, ".ag-fleet-compact .ag-bot-card"), probe(page, ".ag-fleet-compact .ag-card-actions")
    assert compact_actions["bottom"] <= compact_card["bottom"] and compact_actions["left"] < compact_card["right"]
    for selector in ('.ag-fleet-compact .ag-card-actions button[data-ag="open-chat"]',
                     '.ag-fleet-compact .ag-card-actions button[data-ag="stop-chat"]'):
        button = probe(page, selector)
        assert button["width"] >= 44 and button["height"] >= 44, (selector, button)
        assert contrast(page, selector) >= 4.5, selector
    # Latest activity is muted like the duration, and still readable.
    assert probe(page, ".ag-fleet-compact .ag-row-latest")["color"] == probe(page, ".ag-row-dur")["color"]
    assert contrast(page, ".ag-fleet-compact .ag-row-latest") >= 4.5
    page.focus(".ag-fleet-compact .ag-card-actions button")
    assert page.evaluate("document.activeElement.matches('.ag-fleet-compact .ag-card-actions button')")
    assert_no_sideways_scroll(page, ".agents-modal-content")


@pytest.mark.parametrize("width", [700, 390])
def test_compact_fleet_and_chat_agent_strip_stay_distinct(open_app, width):
    page = open_app(width)
    assert page.evaluate("document.getElementById('sidebar-command-menu').hidden") is True
    page.wait_for_function("!document.getElementById('agent-strip').hidden")
    toggle = page.locator(".agent-strip-toggle")
    page.evaluate("document.querySelector('.agent-strip-toggle').click()")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    settle(page, ".agent-strip")
    assert_usable(page, ".agent-strip-toggle")
    assert probe(page, ".agent-strip-head")["height"] < 55
    page.evaluate("document.querySelector('.agent-strip-toggle').click()")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    settle(page, ".agent-strip")
    assert probe(page, ".agent-strip-row")["height"] <= 130
    assert_no_sideways_scroll(page)

    open_agents(page)
    page.wait_for_function("document.querySelector('.ag-fleet').classList.contains('ag-fleet-compact')")
    for card in boxes(page, ".ag-fleet-compact .ag-bot-card"):
        assert card["height"] <= (155 if width <= 390 else 110), card
        assert card["left"] >= 0 and card["right"] <= width + 1, card
    for action in boxes(page, ".ag-fleet-compact .ag-card-actions button"):
        assert action["height"] >= 44 and action["width"] >= 44 and action["right"] <= width + 1, action
    assert_no_sideways_scroll(page, ".agents-modal-content")


@pytest.mark.parametrize("style", ["classic", "agamemnon"])
def test_phone_soldier_chooser_precedes_settings_and_scrolls_with_the_sheet(open_app, style):
    page = open_app(390, style=style)
    open_agents(page)
    select_agent(page, PARENT_ID)
    positions = page.evaluate("(() => { const p = document.querySelector('#ag-panel-overview');"
                              " return {appearance: p.querySelector('.ag-appearance').getBoundingClientRect().top,"
                              " settings: p.querySelector('.ag-loadout-summary').getBoundingClientRect().top,"
                              " overflow: getComputedStyle(p).overflowY,"
                              " detail: getComputedStyle(document.querySelector('#ag-detail')).overflowY}; })()")
    assert positions["appearance"] < positions["settings"], positions
    assert positions["overflow"] == "visible" and positions["detail"] == "auto", positions
    scout = "label:has(#ag-appearance-scout)"
    page.evaluate("document.querySelector('#ag-appearance-scout').scrollIntoView({block: 'center'})")
    settle(page, scout)
    assert_usable(page, scout)
    # The chooser's heading names the lead and sticks to the top of the
    # detail sheet above the options.
    context = page.evaluate("""() => { const d = document.querySelector('#ag-detail'), c = d.querySelector('.ag-soldier-context');
      const option = d.querySelector('label:has(#ag-appearance-scout)'), r = c.getBoundingClientRect(),
            a = option.getBoundingClientRect(), b = d.getBoundingClientRect();
      return {label: c.textContent.trim(), top: r.top, bottom: r.bottom, detailTop: b.top, detailBottom: b.bottom,
              optionTop: a.top, position: getComputedStyle(c).position,
              atPoint: c.contains(document.elementFromPoint(r.left + 20, r.top + 10))}; }""")
    assert "Lead engineer" in context["label"], context
    assert context["position"] == "sticky" and context["detailTop"] <= context["top"] < context["detailBottom"], context
    assert context["bottom"] <= context["optionTop"] + 1 and context["atPoint"], context


def _rgb(value: str) -> list[float]:
    """Chromium's computed rgb() or color(srgb ...), as 0..1 channels."""
    numbers = [float(v) for v in re.findall(r"(?<![a-z])[+-]?\d+(?:\.\d+)?", value)]
    if value.startswith("color(srgb"):
        return numbers[:3]
    assert value.startswith("rgb("), value
    return [v / 255 for v in numbers[:3]]


def _luminance(rgb: list[float]) -> float:
    linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in rgb]
    return sum(v * weight for v, weight in zip(linear, [.2126, .7152, .0722]))


@pytest.mark.parametrize("style", ["classic", "agamemnon"])
@pytest.mark.parametrize("theme", [ODYSSEUS_THEME, LIGHT_THEME], ids=["odysseus", "light"])
def test_desktop_metadata_contrast_and_control_targets(open_app, style, theme):
    page = open_app(1440, theme=theme, style=style)
    open_agents(page)
    select_agent(page, PARENT_ID)
    # The solid reading panel is the backing for transparent metadata text.
    paints = page.evaluate("""() => { const box = document.querySelector('#agents-dashboard');
      const sample = document.createElement('span'); sample.style.color = 'var(--panel)'; box.appendChild(sample);
      const backing = getComputedStyle(sample).color; sample.remove();
      return {panel: backing, entries: ['.ag-detail-meta', '.ag-row-latest', '.ag-row-dur', '.ag-appearance p']
        .map(s => [s, getComputedStyle(box.querySelector(s)).color])}; }""")
    for selector, fg in paints["entries"]:
        a, b = sorted([_luminance(_rgb(fg)), _luminance(_rgb(paints["panel"]))])
        assert (b + .05) / (a + .05) >= 4.5, (selector, fg, paints["panel"])
    small = page.evaluate("""[...document.querySelectorAll('#agents-dashboard button')]
      .filter(e => e.getBoundingClientRect().width && e.getBoundingClientRect().height)
      .map(e => ({className: e.className, width: e.getBoundingClientRect().width, height: e.getBoundingClientRect().height}))
      .filter(x => x.width < 24 || x.height < 24)""")
    assert not small, small


@pytest.mark.parametrize("theme", [LIGHT_THEME, ODYSSEUS_THEME], ids=["light", "odysseus"])
def test_classic_phalanx_header_shows_the_product_helmet(open_app, theme):
    page = open_app(1440, theme=theme)
    assert page.evaluate("document.documentElement.dataset.style") == "classic"
    open_agents(page)
    expect(page.locator("#agents-dashboard .ag-window-antenna")).to_have_count(0)
    mark, title = probe(page, "#agents-dashboard .ag-window-mark"), probe(page, "#agents-dashboard .ag-window-title")
    assert mark["visible"] and 22 <= mark["width"] <= 32 and mark["width"] == mark["height"], mark
    assert mark["right"] <= title["left"], (mark, title)
    assert mark["top"] >= probe(page, "#agents-dashboard .agents-window-header")["top"], mark
    paint = page.evaluate("(sel) => { const cs = getComputedStyle(document.querySelector(sel));"
                          " return {mask: cs.webkitMaskImage || cs.maskImage, bg: cs.backgroundColor, border: cs.borderTopWidth}; }",
                          "#agents-dashboard .ag-window-mark")
    assert "agamemnon-trojan-helmet.svg" in paint["mask"], paint
    assert paint["bg"] == page.evaluate("getComputedStyle(document.querySelector('.sidebar-brand-icon')).backgroundColor"), paint
    assert paint["border"] == "0px", paint


def test_classic_close_button_draws_one_glyph(open_app):
    page = open_app(1440, style="classic")
    open_agents(page)
    close = page.evaluate("(() => { const b = document.getElementById('close-agents-dashboard');"
                          " return {text: b.textContent.trim(), font: getComputedStyle(b).fontSize,"
                          " pseudo: getComputedStyle(b, '::before').content}; })()")
    # The text glyph is hidden (0px) and the ::before draws the only one.
    assert close["text"] == "×" and close["font"] == "0px" and close["pseudo"] != "none", close


# Every drawn soldier and the slot it is meant to sit in.
SPRITE_FIT_JS = r"""
(slotSelector) => [...document.querySelectorAll(slotSelector)].filter(s => s.offsetParent).map(slot => {
  const sprite = slot.querySelector('.ag-soldier-sprite');
  const s = sprite.getBoundingClientRect(), b = slot.getBoundingClientRect();
  return {slot: {left: b.left, right: b.right, width: b.width},
          sprite: {left: s.left, right: s.right, width: s.width, height: s.height},
          display: getComputedStyle(sprite).display};
})
"""


def assert_sprites_fit(page, slot_selector: str, *, min_fill: float, min_px: float = 16) -> list:
    page.wait_for_selector(slot_selector, state="attached")
    fits = page.evaluate(SPRITE_FIT_JS, slot_selector)
    assert fits, f"no soldiers drawn in {slot_selector}"
    for fit in fits:
        slot, sprite = fit["slot"], fit["sprite"]
        assert fit["display"] != "none", fit
        assert sprite["left"] >= slot["left"] - 1 and sprite["right"] <= slot["right"] + 1, (slot_selector, fit)
        assert sprite["width"] >= max(min_px, slot["width"] * min_fill), (slot_selector, fit)
        assert abs(sprite["width"] - sprite["height"]) <= 1, (slot_selector, fit)
    return fits


def toggle_density(page, expanded: bool) -> None:
    page.click('[data-ag="fleet-density"]')
    page.wait_for_function("(c) => document.querySelector('.ag-fleet').classList.contains(c)",
                           arg="ag-fleet-expanded" if expanded else "ag-fleet-compact")
    settle(page, ".ag-fleet")


@pytest.mark.parametrize("width", [1440, 700, 390])
def test_soldier_sprites_fit_compact_large_and_detail_slots(open_app, width):
    page = open_app(width)
    open_agents(page)
    page.wait_for_function("document.querySelector('.ag-fleet').classList.contains('ag-fleet-compact')")
    assert_sprites_fit(page, "#agents-dashboard .ag-card-avatar", min_fill=0.85, min_px=28)
    toggle_density(page, expanded=True)
    assert_sprites_fit(page, "#agents-dashboard .ag-card-avatar", min_fill=0.85, min_px=44)
    select_agent(page, PARENT_ID)
    settle(page, "#ag-detail .ag-console-robot-bay")
    assert_sprites_fit(page, "#ag-detail .ag-console-robot-bay", min_fill=0.6, min_px=48)
    if page.evaluate("!!document.querySelector('#ag-detail .ag-child .ag-bot')"):
        assert_sprites_fit(page, "#ag-detail .ag-child .ag-bot", min_fill=0.9, min_px=20)


def test_docked_large_soldiers_stay_inside_their_column(open_app):
    page = open_app(1440)
    open_agents(page)
    toggle_density(page, expanded=True)  # large cards: the docked geometry must still win
    page.click("#ag-dock-right")
    page.wait_for_function("document.getElementById('agents-dashboard').classList.contains('modal-right-docked')")
    settle(page, ".agents-modal-content")
    fits = assert_sprites_fit(page, "#agents-dashboard .ag-card-avatar", min_fill=0.85, min_px=28)
    copies = page.evaluate("[...document.querySelectorAll('#agents-dashboard .ag-card-avatar')].filter(c => c.offsetParent)"
                           ".map(c => c.parentElement.querySelector('.ag-card-copy').getBoundingClientRect().left)")
    for fit, left in zip(fits, copies):
        assert fit["sprite"]["right"] <= left + 1, (fit, left)


def test_large_card_actions_do_not_break_words(open_app):
    page = open_app(1440)
    open_agents(page)
    toggle_density(page, expanded=True)
    buttons = page.evaluate("[...document.querySelectorAll('.ag-fleet-expanded .ag-card-actions .wb-icon-btn')]"
                            ".filter(b => b.offsetParent).map(b => { const r = b.getBoundingClientRect();"
                            " return {width: r.width, height: r.height, right: r.right,"
                            " whiteSpace: getComputedStyle(b).whiteSpace}; })")
    assert buttons
    for button in buttons:
        assert button["whiteSpace"] == "nowrap" and button["width"] >= 44, button
        assert button["height"] <= 48 and button["right"] <= 1441, button


@pytest.mark.parametrize("width", [390, 320])
def test_classic_phone_toolbar_wraps_and_card_actions_stay_touchable(open_app, width):
    page = open_app(width, theme=LIGHT_THEME)
    assert page.evaluate("document.documentElement.dataset.style") == "classic"
    open_agents(page)
    assert page.evaluate("(() => { const h = document.querySelector('#agents-dashboard .ag-head');"
                         " return h.scrollWidth <= h.clientWidth + 1; })()")
    head = boxes(page, "#agents-dashboard .ag-head-actions button")
    assert head and all(b["left"] >= 0 and b["right"] <= width + 1 for b in head), head
    actions = boxes(page, ".ag-fleet-compact .ag-card-actions .wb-icon-btn")
    assert actions and all(a["width"] >= 24 and a["height"] >= 24 and a["right"] <= width + 1 for a in actions), actions


@pytest.mark.parametrize("width", [1440, 390])
def test_classic_soldier_picker_options_fit_the_detail_pane(open_app, width):
    page = open_app(width, theme=LIGHT_THEME)
    open_agents(page)
    select_agent(page, PARENT_ID)
    page.wait_for_selector("#ag-detail .ag-appearance-options svg", state="attached")
    choices = page.evaluate("[...document.querySelectorAll('#ag-detail .ag-appearance-option svg')]"
                            ".filter(svg => getComputedStyle(svg).display !== 'none')"
                            ".map(svg => { const r = svg.getBoundingClientRect(), c = svg.closest('label').getBoundingClientRect();"
                            " return {width: r.width, height: r.height, left: r.left, right: r.right,"
                            " containerLeft: c.left, containerRight: c.right}; })")
    assert len(choices) >= 5, choices
    for choice in choices:
        assert 25 <= choice["width"] <= 52 and choice["height"] <= 52, choice
        assert choice["containerLeft"] - 1 <= choice["left"] and choice["right"] <= choice["containerRight"] + 1, choice
    # No icon and legend collision, even for the Automatic option's seal.
    assert page.evaluate("(() => { const l = document.querySelector('.ag-appearance-option');"
                         " const icon = l.querySelector('.ag-bot').getBoundingClientRect();"
                         " const label = l.querySelector(':scope > span:last-child').getBoundingClientRect();"
                         " return icon.bottom + 3 <= label.top; })()")
    assert_usable(page, "#close-agents-dashboard")
    if width <= 390:
        assert_no_sideways_scroll(page, ".agents-modal-content")
        page.locator(".ag-appearance-option:last-child").scroll_into_view_if_needed()
        assert probe(page, ".ag-appearance-option:last-child")["visible"]
        page.focus("#close-agents-dashboard")
        assert page.evaluate("document.activeElement.id") == "close-agents-dashboard"


def test_classic_picker_at_125_percent_zoom_keeps_close_and_choices(open_app):
    page = open_app(390, theme=LIGHT_THEME)
    page.evaluate("document.documentElement.style.zoom = '1.25'")
    open_agents(page)
    select_agent(page, PARENT_ID)
    settle(page, "#close-agents-dashboard")
    assert_usable(page, "#close-agents-dashboard")
    assert_no_sideways_scroll(page, ".agents-modal-content")


def test_saved_soldier_survives_a_style_switch_and_a_reload(open_app, static_app):
    static_app.state.appearance[PARENT_ID] = "reviewer"
    page = open_app(390, style="classic")
    open_agents(page)
    assert page.evaluate("document.documentElement.dataset.style") == "classic"
    lead_variant(page).to_have_attribute("data-soldier-variant", "reviewer")
    # Phones show one close control, the drawn one.
    assert page.evaluate("getComputedStyle(document.querySelector('#close-agents-dashboard')).fontSize") == "0px"
    set_checkbox(page, "#theme-style-toggle", True)
    page.wait_for_function("document.documentElement.dataset.style === 'agamemnon'")
    lead_variant(page).to_have_attribute("data-soldier-variant", "reviewer")
    page.reload()
    wait_ready(page)
    assert page.evaluate("document.documentElement.dataset.style") == "agamemnon"
    open_agents(page)
    lead_variant(page).to_have_attribute("data-soldier-variant", "reviewer")
