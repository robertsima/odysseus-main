"""Turn wall time sums server-measured completed rounds."""
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_turn_duration_sums_completed_rounds_and_formats_total():
    source = (ROOT / 'static/js/roundTiming.js').read_text()
    script = source.replace('export ', '') + '''
console.log(JSON.stringify([durationLabel(undefined),durationLabel(null),
durationLabel(-1),durationLabel(0),durationLabel(2.34),
durationLabel(65),durationLabel(3665),totalTurnDuration([63.24,5]),
totalTurnDuration([63.24,null,5]),totalTurnDuration([null,undefined]),
totalTurnDuration('legacy')]));
'''
    result = subprocess.run(['node', '--input-type=module'], input=script, text=True,
                            capture_output=True, check=True)
    assert json.loads(result.stdout) == [
        '', '', '', '0.0s', '2.3s', '1m 5s', '1h 1m', 68.24, 68.24, None, None,
    ]


def test_saved_and_streamed_turns_sum_server_round_timing():
    renderer = (ROOT / 'static/js/chatRenderer.js').read_text()
    chat = (ROOT / 'static/js/chat.js').read_text()
    route = (ROOT / 'routes/chat_routes.py').read_text()
    assert 'showTurnDuration(firstMsgAi, totalTurnDuration(metadata?.round_durations_s))' in renderer
    assert 'showTurnDuration(wrap, totalTurnDuration(metadata?.round_durations_s))' in renderer
    assert "json.type === 'round_complete'" in chat
    assert 'completedRoundDurations.push(json.duration_s)' in chat
    assert 'totalTurnDuration(completedRoundDurations)' in chat
    assert 'turnTimingTarget = holder' in chat
    assert 'completedRoundDurations = []' in chat
    assert '"round_durations_s": [_round_durations.get(i)' in route
