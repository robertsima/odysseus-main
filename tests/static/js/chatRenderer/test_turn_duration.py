"""User-visible combined elapsed time for a completed assistant turn."""
from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from tests.helpers.static_app import HISTORY, SESSION_ID, expect, wait_ready

ROOT = Path(__file__).resolve().parents[4]
pytestmark = pytest.mark.browser


def test_turn_duration_sums_completed_rounds_and_formats_total():
    module_url = (ROOT / "static/js/roundTiming.js").as_uri()
    script = f"""
import {{durationLabel, totalTurnDuration}} from {json.dumps(module_url)};
console.log(JSON.stringify([
  durationLabel(undefined), durationLabel(null), durationLabel(-1),
  durationLabel(0), durationLabel(2.34), durationLabel(65),
  durationLabel(3665), totalTurnDuration([63.24, 5]),
  totalTurnDuration([63.24, null, 5]), totalTurnDuration([null, undefined]),
  totalTurnDuration('legacy')
]));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout) == [
        "", "", "", "0.0s", "2.3s", "1m 5s", "1h 1m",
        68.24, 68.24, None, None,
    ]


def test_history_shows_one_combined_turn_duration_badge(new_page, static_app):
    history = copy.deepcopy(HISTORY)
    history["history"][1]["metadata"] = {"round_durations_s": [63.24, 5]}
    page = new_page(390)
    page.route("**/api/history/*", lambda route: route.fulfill(json=history))
    page.goto(static_app.url + f"/#{SESSION_ID}")
    wait_ready(page, chat=False)

    badges = page.locator("#chat-history .agent-turn-duration")
    expect(badges).to_have_count(1)
    expect(badges).to_have_text("Turn · 1m 8s")
    expect(page.locator("#chat-history .msg-ai .agent-turn-duration")).to_have_count(1)
    expect(page.locator("#chat-history .msg:last-child .agent-turn-duration")).to_have_count(0)
