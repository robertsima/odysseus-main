"""Native select menus (static/style.css): the drop-down list a themed
``<select>`` opens is drawn by the browser, so its options need their own
colours or they come out as dark text on a dark panel (or the reverse).
"""
from __future__ import annotations

import pytest

from tests.helpers.static_app import LIGHT_THEME, ODYSSEUS_THEME

pytestmark = pytest.mark.browser

OPTION_COLOURS_JS = """
() => {
  const parse = (c) => {
    const nums = (c.match(/[\\d.]+/g) || []).slice(0, 3).map(Number);
    return c.startsWith('color(') ? nums.map((x) => x * 255) : nums;
  };
  const lum = (rgb) => { const v = rgb.map((x) => { x /= 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]; };
  const option = document.querySelector('#theme-font-select option:not(:checked)');
  const bg = lum(parse(getComputedStyle(option).backgroundColor));
  const fg = lum(parse(getComputedStyle(option).color));
  return {bg, fg, ratio: (Math.max(bg, fg) + 0.05) / (Math.min(bg, fg) + 0.05)};
}
"""


@pytest.mark.parametrize("theme, light", [(None, False), (ODYSSEUS_THEME, False), (LIGHT_THEME, True)])
def test_native_select_options_are_readable_in_every_colourway(open_app, theme, light):
    page = open_app(1440, chat=False, theme=theme)
    colours = page.evaluate(OPTION_COLOURS_JS)
    assert colours["ratio"] >= 4.5, colours
    # The option list is on the same side of the lightness scale as the page.
    assert (colours["bg"] > colours["fg"]) == light, colours
