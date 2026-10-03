"""Real chat and Phalanx render with historical, waiting and nested workers."""
import json
import copy
from pathlib import Path

import pytest

from tests.helpers import agamemnon_browser as fixtures

pytestmark = pytest.mark.skipif(not fixtures.chromium_path(), reason='Chromium required')


@pytest.mark.parametrize('style,width', [('agamemnon', 1440), ('agamemnon', 390), ('classic', 390)])
def test_nested_tree_and_round_duration_render(style, width):
    rows = fixtures.AGENT_ROWS
    history = fixtures.HISTORY['history']
    original = copy.deepcopy(rows), copy.deepcopy(history)
    rows[0]['status'] = 'needs_input'
    rows[0]['children_running'] = 0
    rows[1]['status'] = 'waiting_approval'
    rows.extend([
        {'session_id': 'agent-grandchild', 'name': 'Catalog probe', 'status': 'running',
         'source': 'session', 'parent_session': fixtures.WORKER_ID, 'model': 'openai/gpt-5', 'config': {}},
        {'session_id': 'agent-old', 'name': 'Old audit', 'status': 'finished',
         'source': 'session', 'parent_session': fixtures.PARENT_ID, 'model': 'openai/gpt-5', 'config': {}},
    ])
    history[1] = {**history[1], 'metadata': {'round_texts': ['Checked the migration.', 'Outlined risks.'],
                                         'round_durations_s': [63.24, 5.0]}}
    page = None
    try:
        with fixtures.StaticAppServer() as server:
            browser = fixtures.launch_chromium(fixtures.chromium_path())
            try:
                page = browser.page(width, 844 if width < 500 else 920)
                page.before_load("localStorage.setItem('odysseus-page-style-v1', " +
                                 json.dumps(json.dumps({'value': style, 'updated_at': 1})) + ");")
                page.goto(f'{server.url}/#{fixtures.SESSION_ID}', settle=0.7)
                page.wait_for("document.querySelectorAll('#chat-history .msg').length >= 4")
                page.wait_for("document.querySelector('.agent-round-duration')")
                assert '1m 3s' in page.eval("document.querySelector('#chat-history .agent-round-duration').textContent")
                page.eval('window.agentsDashboard.open()')
                page.wait_for("document.querySelector('.ag-card[data-sid=\"agent-grandchild\"]')")
                cards = page.eval("[...document.querySelectorAll('#agents-dashboard .ag-card')].map(c => ({id:c.dataset.sid,status:c.textContent,indent:c.getBoundingClientRect().left}))")
                by_id = {c['id']: c for c in cards}
                assert by_id['agent-grandchild']['indent'] > by_id[fixtures.WORKER_ID]['indent'] > by_id[fixtures.PARENT_ID]['indent']
                assert 'Working' in by_id['agent-grandchild']['status']
                assert 'approval' in by_id[fixtures.WORKER_ID]['status'].lower()
                if 'agent-old' in by_id:
                    assert 'Working' not in by_id['agent-old']['status']
                # A descendant search keeps its ancestor context, but not an
                # unrelated sibling or a stale branch.
                page.eval("document.querySelector('#ag-filter').value = 'Catalog probe'; document.querySelector('#ag-filter').dispatchEvent(new Event('input',{bubbles:true}))")
                page.wait_for("document.querySelectorAll('#agents-dashboard .ag-card').length === 3")
                assert set(page.eval("[...document.querySelectorAll('#agents-dashboard .ag-card')].map(c=>c.dataset.sid)")) == {
                    fixtures.PARENT_ID, fixtures.WORKER_ID, 'agent-grandchild'}
                page.eval("document.querySelector('#ag-filter').value = ''; document.querySelector('#ag-filter').dispatchEvent(new Event('input',{bubbles:true}))")
                page.wait_for("document.querySelector('.ag-card[data-sid=\"agent-grandchild\"]')")
                page.eval("document.querySelector('.ag-card[data-sid=\"agent-grandchild\"]').scrollIntoView({block:'center',behavior:'instant'})")
                out = Path('.visual-check')
                out.mkdir(exist_ok=True)
                page.screenshot(out / f'tree-timing-{style}-{width}.png')
            finally:
                if page: page.close()
                browser.close()
    finally:
        rows[:], history[:] = original


def test_chat_run_card_reconciles_finished_poll_without_finish_event():
    with fixtures.StaticAppServer() as server:
        browser = fixtures.launch_chromium(fixtures.chromium_path())
        page = browser.page(390, 844)
        try:
            page.goto(f'{server.url}/#{fixtures.SESSION_ID}', settle=0.7)
            page.wait_for("document.querySelector('.agent-run-card.running')", timeout=12)
            server.state.run_status = 'finished'
            page.wait_for("document.querySelector('.agent-run-card:not(.running)')", timeout=15)
            assert 'finished' in page.eval("document.querySelector('.agent-run-card').textContent").lower()
        finally:
            page.close()
            browser.close()


def test_single_round_history_shows_duration_but_legacy_reply_does_not():
    history = fixtures.HISTORY['history']
    original = copy.deepcopy(history)
    history[1] = {**history[1], 'metadata': {'round_durations_s': [2.34]}}
    try:
        with fixtures.StaticAppServer() as server:
            browser = fixtures.launch_chromium(fixtures.chromium_path())
            page = browser.page(390, 844)
            try:
                page.goto(f'{server.url}/#{fixtures.SESSION_ID}', settle=0.7)
                page.wait_for("document.querySelectorAll('#chat-history .msg').length >= 4")
                page.wait_for("document.querySelector('#chat-history .agent-round-duration')")
                assert '2.3s' in page.eval("document.querySelector('#chat-history .agent-round-duration').textContent")
                assert page.eval("document.querySelectorAll('#chat-history .agent-round-duration').length") == 1
            finally:
                page.close()
                browser.close()
    finally:
        history[:] = original
