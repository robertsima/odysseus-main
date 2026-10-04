"""Completed round wall time must not be fabricated for legacy messages."""
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_round_label_units_and_missing_history():
    source = (ROOT / 'static/js/roundTiming.js').read_text()
    script = source.replace('export ', '') + '''
console.log(JSON.stringify([roundDurationLabel(undefined),roundDurationLabel(null),
roundDurationLabel(-1),roundDurationLabel(0),roundDurationLabel(2.34),
roundDurationLabel(65),roundDurationLabel(3665)]));
'''
    result = subprocess.run(['node', '--input-type=module'], input=script, text=True,
                            capture_output=True, check=True)
    assert json.loads(result.stdout) == ['', '', '', '0.0s', '2.3s', '1m 5s', '1h 1m']


def test_saved_and_streamed_rounds_share_server_timing():
    renderer = (ROOT / 'static/js/chatRenderer.js').read_text()
    chat = (ROOT / 'static/js/chat.js').read_text()
    route = (ROOT / 'routes/chat_routes.py').read_text()
    assert 'showRoundDuration(wrap, metadata.round_durations_s?.[r])' in renderer
    assert 'showRoundDuration(threadWrap, metadata.round_durations_s?.[r])' in renderer
    assert "showRoundDuration(wrap, metadata?.round_durations_s?.[0])" in renderer
    assert "json.type === 'round_complete'" in chat
    assert '"round_durations_s": [_round_durations.get(i)' in route
