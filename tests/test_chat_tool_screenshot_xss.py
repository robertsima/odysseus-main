"""Regression guard for the compare probe's provider labels.

The other DOM sinks this file once pinned are behavior tests now:
tests/static/js/chat/tool_events.test.mjs, chat/role_labels.test.mjs,
chatRenderer/image_sources.test.mjs, compare/stream/pane_stream.test.mjs and
group/group_chat.test.mjs. This one sits at the end of the compare selector's
search-mode flow (pick providers and synthesis models, Start, probe), which
belongs to the browser tier.
"""

from pathlib import Path


_REPO = Path(__file__).resolve().parent.parent


def test_compare_probe_provider_labels_are_escaped():
    selector = (_REPO / "static" / "js" / "compare" / "selector.js").read_text(encoding="utf-8")

    assert "${escapeHtml(p.label || p.id)}" in selector
    assert "${p.label || p.id}" not in selector
