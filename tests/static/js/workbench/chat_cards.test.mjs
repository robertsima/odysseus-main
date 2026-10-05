// Agent run cards in the chat (static/js/workbench.js). A worker's card is live
// DOM, not saved history. When the chat re-renders its history (switching back
// to the chat, or the reload after a background run ends) the cards would be
// wiped; the workbench puts them back once sessions.js announces the render.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

globalThis.EventSource = class EventSource {
  addEventListener() {}
  removeEventListener() {}
  close() {}
};
window.sessionModule = { getCurrentSessionId: () => 's1' };
document.body.innerHTML = '<div id="chat-history"><div class="msg msg-user">start the scan</div></div>';
fake.route('GET', '/api/auth/settings', () => ({ workbench_enabled: true }));

const workbench = (await import('../../../../static/js/workbench.js')).default;
const { _state: state } = await import('../../../../static/js/workbench.js');
await workbench.init();

function runningWorker(id) {
  return {
    run_id: id, source: 'session', session_id: 's1', title: `worker ${id}`, status: 'running',
    started_at: 0, finished_at: null, events: [], data: {}, tools: 0, errors: 0,
  };
}

function historyRendered() {
  document.dispatchEvent(new CustomEvent('odysseus:history-rendered', { detail: { sessionId: 's1' } }));
}

const history = () => document.getElementById('chat-history');

test('a running worker gets a card when the chat is opened', () => {
  state.runs.set('r1', runningWorker('r1'));

  historyRendered();

  assert.equal(history().querySelectorAll('.agent-run-card[data-run="r1"]').length, 1);
});

test('the card comes back after the chat history is re-rendered', () => {
  history().innerHTML = '<div class="msg msg-user">start the scan</div><div class="msg msg-assistant">on it</div>';
  assert.ok(!history().querySelector('.agent-run-card'), 'the re-render wiped the card');

  historyRendered();

  assert.equal(history().querySelectorAll('.agent-run-card[data-run="r1"]').length, 1);
});

test('a render of another chat leaves the cards alone', () => {
  history().innerHTML = '';

  document.dispatchEvent(new CustomEvent('odysseus:history-rendered', { detail: { sessionId: 'other' } }));

  assert.ok(!history().querySelector('.agent-run-card'));
});
