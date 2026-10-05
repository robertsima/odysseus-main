"""Regression: the Qwen bare-marker scrub must not eat a lone `end` (#5547).

`_QWEN_BARE_MARKER_RE` cleans Qwen turn markers that leak into content. Its
`end` branch was `\\|?end\\|?` — both pipes optional — so it also matched a bare
`end` surrounded by whitespace and replaced it with a space. Any message
containing Ruby, Lua or shell code that closes a block with a lone `end` had
those lines silently deleted, in the stored text and in the rendered message.

Requiring at least one pipe keeps every real marker (`|end`, `end|`, `|end|`,
`/|end|`) stripping as before. The same pattern is duplicated in
static/js/chatRenderer.js; tests/static/js/chatRenderer/bare_end_marker.test.mjs
runs the same cases against it.

"""
import pytest

import src.agent_tools  # noqa: F401  (break agent_tools<->tool_parsing import cycle)
from src.tool_parsing import strip_tool_blocks

# Inputs that must survive untouched, and the substring that proves they did.
KEPT = [
    ("loop do\n    puts \"yo\"\nend\n", "\nend"),          # the reported Ruby case
    ("if x then\nend", "\nend"),
    ("function f()\nend\n", "\nend"),
    ("a end b", "a end b"),
    ("append end", "append end"),
    ("END", "END"),
    ("\nEnd\n", "End"),
    ("x assistant y", "x assistant y"),          # mid-sentence must survive (#5971)
]

# Real markers — at least one pipe, plus the role word — with the exact output
# they must still produce. Asserted as equality rather than "marker not in out"
# so narrowing the pattern can't pass by deleting more than it should.
STRIPPED = [
    ("a |end| b", "a  b"),
    ("a /|end| b", "a  b"),
    ("a |end b", "a  b"),
    ("a end| b", "a  b"),
    ("Before\nassistant\nAfter", "Before \nAfter"),   # bare-marker on its own line still stripped
    ("Before\n  assistant\t \nAfter", "Before \nAfter"),     # whitespace-padded marker still stripped
    ("Before\n\tassistan  \nAfter", "Before \nAfter"),       # truncated marker variant still stripped
]


@pytest.mark.parametrize("text,kept", KEPT)
def test_bare_end_survives_stripping(text, kept):
    assert kept in strip_tool_blocks(text)


@pytest.mark.parametrize("text,expected", STRIPPED)
def test_piped_end_markers_are_still_stripped(text, expected):
    assert strip_tool_blocks(text) == expected


def test_bare_end_inside_a_fenced_block_survives():
    """The scrub runs over the whole message, fenced regions included."""
    out = strip_tool_blocks("Here:\n```ruby\nloop do\n  puts 1\nend\n```\nDone.")
    assert "\nend\n" in out
