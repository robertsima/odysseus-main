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
    history[1] = {**history[1], 'content': 'Checked the migration.\n\nOutlined risks.',
                  'metadata': {'round_texts': ['Checked the migration.', 'Outlined risks.'],
                               'round_durations_s': [63.24, 5.0]}}
    page = None
    try:
        with fixtures.StaticAppServer() as server:
            browser = fixtures.launch_chromium(fixtures.chromium_path())
            try:
                page = browser.page(width, 1180 if width < 500 else 920)
                page.before_load("localStorage.setItem('odysseus-page-style-v1', " +
                                 json.dumps(json.dumps({'value': style, 'updated_at': 1})) + ");")
                page.goto(f'{server.url}/#{fixtures.SESSION_ID}', settle=0.7)
                page.wait_for("document.querySelectorAll('#chat-history .msg').length >= 4")
                page.wait_for("document.querySelector('.agent-round-duration')")
                assert '1m 3s' in page.eval("document.querySelector('#chat-history .agent-round-duration').textContent")
                assert page.eval("[...document.querySelectorAll('#chat-history .agent-round-duration')].map(n=>n.textContent.trim())") == ['Round · 1m 3s', 'Round · 5.0s']
                assert page.eval("!document.querySelector('#chat-history .msg:last-child .agent-round-duration')")
                out = Path('.visual-check')
                out.mkdir(exist_ok=True)
                page.eval("document.querySelector('#chat-history .agent-round-duration').scrollIntoView({block:'center',behavior:'instant'})")
                page.screenshot(out / f'chat-multi-round-{style}-{width}.png')
                page.eval('window.agentsDashboard.open()')
                page.wait_for("document.querySelector('.ag-card[data-sid=\"agent-grandchild\"]')")
                summary = page.eval("document.querySelector('#ag-window-summary').textContent")
                assert '1 active · 2 waiting · 2 completed today' in summary
                assert page.eval("[...document.querySelectorAll('#agents-dashboard .ag-seg')].map(b=>b.textContent.trim())") == ['Needs you2', 'Active1', 'Recent']
                assert page.eval("document.querySelector('#agents-dashboard .ag-group-attn .wb-count').textContent.trim()") == '1 tree'
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
                toggle = f'.ag-workers-toggle[data-sid="{fixtures.PARENT_ID}"]'
                page.wait_for(f"document.querySelector({json.dumps(toggle)})")
                assert page.eval(f"document.querySelector({json.dumps(toggle)}).getAttribute('aria-expanded')") == 'false'
                assert page.eval(f"!!document.getElementById(document.querySelector({json.dumps(toggle)}).getAttribute('aria-controls'))")
                # Native button provides keyboard activation; focus survives
                # the fleet re-render when it is activated.
                page.eval(f"document.querySelector({json.dumps(toggle)}).focus()")
                page.eval(f"document.querySelector({json.dumps(toggle)}).click()")
                page.wait_for(f"document.querySelector({json.dumps(toggle)})?.getAttribute('aria-expanded') === 'true'")
                assert page.eval(f"document.activeElement === document.querySelector({json.dumps(toggle)})")
                assert page.eval("!!document.querySelector('.ag-card[data-sid=\"agent-old\"]')")
                page.eval("document.querySelector('#agents-dashboard .ag-fleet-list').scrollTop = 0")
                page.screenshot(out / f'tree-expanded-top-{style}-{width}.png')
                page.eval(f"document.querySelector('.ag-card[data-sid=\"{fixtures.WORKER_ID}\"]').scrollIntoView({{block:'center',behavior:'instant'}})")
                page.screenshot(out / f'tree-expanded-waiting-{style}-{width}.png')
                # The narrow sheet has a deliberately short fleet viewport;
                # a second frame exposes descendants rather than presenting
                # a scrolled-middle frame as the top of the tree.
                page.eval("document.querySelector('.ag-card[data-sid=\"agent-grandchild\"]').scrollIntoView({block:'center',behavior:'instant'})")
                page.screenshot(out / f'tree-expanded-nested-{style}-{width}.png')
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
    history[1] = {**history[1], 'content': 'Here is the migration plan.',
                  'metadata': {'round_durations_s': [2.34]}}
    try:
        with fixtures.StaticAppServer() as server:
            browser = fixtures.launch_chromium(fixtures.chromium_path())
            page = browser.page(390, 1180)
            try:
                page.goto(f'{server.url}/#{fixtures.SESSION_ID}', settle=0.7)
                page.wait_for("document.querySelectorAll('#chat-history .msg').length >= 4")
                page.wait_for("document.querySelector('#chat-history .agent-round-duration')")
                assert '2.3s' in page.eval("document.querySelector('#chat-history .agent-round-duration').textContent")
                assert page.eval("document.querySelectorAll('#chat-history .agent-round-duration').length") == 1
                assert page.eval("!document.querySelectorAll('#chat-history .msg')[3].querySelector('.agent-round-duration')")
                page.eval("document.querySelector('#chat-history .agent-round-duration').scrollIntoView({block:'center',behavior:'instant'})")
                page.eval("new Promise(resolve => setTimeout(resolve, 400))")
                out = Path('.visual-check')
                out.mkdir(exist_ok=True)
                page.screenshot(out / 'chat-single-round-legacy-agamemnon-390.png')
            finally:
                page.close()
                browser.close()
    finally:
        history[:] = original
