"""The scope header above Changes and Commits (static/js/workbench.js).

It must stay inside its panel in a docked window and in both UI styles, keep
Refresh reachable, and open its Details without moving the layout sideways.
"""
from __future__ import annotations

import pytest

from tests.helpers.static_app import (
    assert_inside_parent, assert_no_sideways_scroll, assert_usable, dock_workbench, open_workbench,
    resize_workbench_dock, settle,
)

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("style", ["agamemnon", "classic"])
@pytest.mark.parametrize("width", [1440, 350])
def test_scope_header_fits_and_names_the_scope(open_app, style, width):
    page = open_app(1440, style=style)
    open_workbench(page, activity=False)
    if width < 1000:
        dock_workbench(page, "right")
        resize_workbench_dock(page, width, "right")
    for tab in ("changes", "commits"):
        page.click(f"#wb-tab-{tab}")
        page.wait_for_selector(f"#wb-{tab} .wb-scope .wb-scope-time")
        text = page.inner_text(f"#wb-{tab} .wb-scope")
        assert "Repository" in text and "Last updated" in text and "Compared with" in text, text
        assert_inside_parent(page, [(".wb-scope", f"#wb-{tab}"), ('.wb-scope [data-wb-act="refresh-repo"]', f"#wb-{tab}")])
        assert_usable(page, f'#wb-{tab} .wb-scope [data-wb-act="refresh-repo"]')
        page.click(f"#wb-{tab} .wb-scope summary")
        page.wait_for_selector(f"#wb-{tab} .wb-scope-dl", state="visible")
        assert "Path" in page.inner_text(f"#wb-{tab} .wb-scope-dl")
        assert_inside_parent(page, [(".wb-scope-dl", f"#wb-{tab}")])
        assert_no_sideways_scroll(page, ".workbench-modal-body")
        settle(page, ".workbench-modal-content")
