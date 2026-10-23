"""The editor's export menu and toolbar dropdowns can be tapped on a phone.

Bug guarded: at phone width the editor pane is a fixed sheet at z-index 10010
while both menus were created at 9999, so they opened underneath the editor.
Nothing showed on the export button, and Review changes, Print as PDF and the
heading/list choices could not be reached.
"""
import pytest

from tests.helpers.static_app import expect

pytestmark = pytest.mark.browser

TEXT = "# Agreement\n\nBetween [CLIENT] and the processor.\n"

# Hit-test the first item: the element under its centre must belong to the menu.
# The mock page's editor is taller than the screen, so the menu opens below the
# fold; it is moved over the editor text first (stacking doesn't depend on where
# a fixed element sits).
ON_TOP = """(sel) => {
    const item = document.querySelector(sel);
    const menu = item.closest('.doc-overflow-menu');
    const text = document.getElementById('doc-editor-textarea').getBoundingClientRect();
    Object.assign(menu.style, {top: (Math.max(text.top, 0) + 40) + 'px', bottom: 'auto'});
    const r = item.getBoundingClientRect();
    const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
    return !!top && menu.contains(top);
}"""


def press(page, selector):
    page.locator(selector).first.evaluate("""e => e.dispatchEvent(
        new MouseEvent('click', {bubbles: true, cancelable: true, clientX: 200, clientY: 300}))""")


def open_doc(open_app, width):
    page = open_app(width, chat=False)
    page.evaluate("""(text) => {
        const m = window.documentModule;
        m.openPanel();
        m.handleDocUpdate({doc_id: 'agr', action: 'create', title: 'Agreement', language: 'markdown', content: text, version: 1});
    }""", TEXT)
    page.wait_for_selector("#doc-footer-export-btn", state="visible")
    return page


@pytest.mark.parametrize("width", [390, 1440])
def test_export_menu_opens_above_the_editor(open_app, width):
    page = open_doc(open_app, width)
    press(page, "#doc-footer-export-btn")
    expect(page.locator("#doc-export-menu .doc-overflow-item").first).to_have_text("Review changes")
    assert page.evaluate(ON_TOP, "#doc-export-menu .doc-overflow-item"), "export menu is under the editor"


@pytest.mark.parametrize("width", [390, 1440])
def test_heading_dropdown_opens_above_the_editor(open_app, width):
    page = open_doc(open_app, width)
    press(page, '.md-dd-toggle[data-dd="heading"]')
    page.wait_for_selector("#doc-md-dd-menu")
    first = "#doc-md-dd-menu > *"
    assert page.evaluate(ON_TOP, first), "heading menu is under the editor"
