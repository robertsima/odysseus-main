"""Regression guard for issue #4002 — clicking the card body outside the
edit textarea collapsed the skill card and silently discarded unsaved edits.

In Brain > Skills, the card's click handler toggles expand/collapse. The
edit <textarea> stops propagation only for clicks landing ON the textarea,
so a click on the surrounding card padding bubbled up to the card handler
and collapsed the card mid-edit — losing the user's changes. The fix bails
out of the card click handler while a `.skill-md-editor` is present, so the
card only leaves edit mode via Save (or the Cancel button added in #3580).

The user-skill card no longer toggles on a card-body click (only its header
button expands it, since PR #57), and its unsaved-draft protection is covered
by tests/test_brain_skill_package_browser.py. The built-in capability card
still toggles on any click, so its guard is pinned here until a node test
of static/js/skills.js replaces this (hygiene allowlist).
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "static/js/skills.js"

# The guard the fix introduces inside the card click handler.
GUARD = re.compile(r"querySelector\(\s*['\"]\.skill-md-editor['\"]\s*\)\s*\)\s*return")


def test_builtin_card_does_not_collapse_while_editing():
    text = SRC.read_text(encoding="utf-8")
    # The built-in capability card has a single handler ending in
    # _expandBuiltinCard; take the click handler that immediately precedes it.
    before = text[: text.index("_expandBuiltinCard(card, b.name)")]
    body = before[before.rindex("card.addEventListener('click'"):]
    assert GUARD.search(body), (
        "built-in capability card click handler must skip collapse while a "
        ".skill-md-editor is present (issue #4002)"
    )
