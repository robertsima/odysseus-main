"""Completed round wall time must not be fabricated for legacy messages.

The label and badge are tested by running static/js/roundTiming.js in
tests/static/js/roundTiming/round_duration.test.mjs, and the rendering from
saved metadata by tests/test_agent_tree_timing_browser.py. What remains here
pins that the chat route records the durations; it is on the hygiene
allowlist until a chat-route behavior test replaces it.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_saved_and_streamed_rounds_share_server_timing():
    renderer = (ROOT / 'static/js/chatRenderer.js').read_text()
    chat = (ROOT / 'static/js/chat.js').read_text()
    route = (ROOT / 'routes/chat_routes.py').read_text()
    assert 'showRoundDuration(wrap, metadata.round_durations_s?.[r])' in renderer
    assert 'showRoundDuration(threadWrap, metadata.round_durations_s?.[r])' in renderer
    assert "showRoundDuration(wrap, metadata?.round_durations_s?.[0])" in renderer
    assert "json.type === 'round_complete'" in chat
    assert '"round_durations_s": [_round_durations.get(i)' in route
