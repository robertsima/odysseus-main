"""The page, its tool windows and the login card share one typeface.

A blank slate showed Space Grotesk on the Agamemnon body, Inter in desktop
tool windows and Fira Code in inputs, and a chosen font changed only the few
rules that read --font-family (2026-10-07).
"""
import pytest

pytestmark = pytest.mark.browser

FAMILIES = """() => {
  const fam = (el) => el ? getComputedStyle(el).fontFamily.split(',')[0].replace(/["']/g, '').trim() : null;
  return {
    body: fam(document.body),
    sidebar: fam(document.querySelector('.sidebar .list-item')),
    window: fam(document.querySelector('#theme-modal .modal-content')),
    composer: fam(document.getElementById('message')),
  };
}"""


@pytest.mark.parametrize("width", [1440, 390])
def test_a_blank_slate_uses_the_agamemnon_typeface_throughout(open_app, width):
    page = open_app(width, chat=False)
    assert page.evaluate("document.documentElement.dataset.style") == "agamemnon"
    families = {k: v for k, v in page.evaluate(FAMILIES).items() if v}
    assert set(families.values()) == {"Space Grotesk"}, families


def test_a_chosen_font_applies_to_the_page_and_its_windows(open_app):
    page = open_app(1440, chat=False)
    page.evaluate("document.getElementById('theme-modal').classList.remove('hidden')")
    page.click('#theme-tabs [data-tab="theme-tab-customize"]')
    page.locator('#theme-font-select').select_option('serif')
    families = {k: v for k, v in page.evaluate(FAMILIES).items() if v}
    assert set(families.values()) == {"Georgia"}, families


def test_the_login_card_matches_a_blank_slate(new_page, static_app):
    page = new_page(1440)
    page.route('**/api/auth/status', lambda route: route.fulfill(
        json={"authenticated": False, "auth_enabled": True}))
    # The harness serves static/ but not the /login route; the declared
    # family and palette are what this checks, not font loading.
    page.goto(static_app.url + "/static/login.html")
    page.wait_for_selector("#submitBtn")
    fonts = page.evaluate("[document.body, document.getElementById('username'), document.getElementById('submitBtn')]"
                          ".map(el => getComputedStyle(el).fontFamily.split(',')[0].replace(/[\"']/g, '').trim())")
    assert set(fonts) == {"Space Grotesk"}, fonts
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(17, 20, 23)"
