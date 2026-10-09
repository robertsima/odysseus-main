"""Live completion must keep the elapsed badge, not just streamed/history badges."""
import copy
import json

import pytest

from tests.helpers.static_app import HISTORY, expect, wait_ready

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('style,width', [('agamemnon', 1440), ('agamemnon', 700),
                                         ('agamemnon', 390), ('classic', 390)])
def test_completed_stream_keeps_visible_duration_and_reload_restores_it(open_app, style, width):
    page = open_app(width, style=style)
    events = [{'delta': 'The migration check is complete.'},
              {'type': 'round_complete', 'round': 1, 'duration_s': 7.34}]
    body = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events) + 'data: [DONE]\n\n'
    page.route('**/api/chat_stream', lambda route: route.fulfill(
        content_type='text/event-stream', body=body))
    page.locator('#message').first.fill('Check this migration.')
    page.locator('.send-btn').first.click()
    page.wait_for_function("document.querySelector('.send-btn').dataset.mode !== 'streaming' && "
                           "document.querySelectorAll('#chat-history .msg-ai').length === 3")
    badge = page.locator('#chat-history .msg-ai').last.locator('.agent-turn-duration')
    expect(badge).to_have_text('Turn · 7.3s')
    expect(badge).to_be_visible()
    bounds = badge.bounding_box()
    host = page.locator('#chat-history .msg-ai').last.bounding_box()
    assert host['x'] <= bounds['x'] and bounds['x'] + bounds['width'] <= host['x'] + host['width'] + 1
    history = copy.deepcopy(HISTORY)
    history['history'].extend([
        {'role': 'user', 'content': 'Check this migration.'},
        {'role': 'assistant', 'content': events[0]['delta'],
         'model': history['model'], 'metadata': {'round_durations_s': [7.34]}}])
    history['total'] = len(history['history'])
    page.route('**/api/history/*', lambda route: route.fulfill(json=history))
    page.reload()
    wait_ready(page, chat=False)
    expect(page.locator('#chat-history .agent-turn-duration')).to_have_count(1)
    expect(page.locator('#chat-history .agent-turn-duration')).to_have_text('Turn · 7.3s')
    expect(page.locator('#chat-history .agent-turn-duration')).to_be_visible()
