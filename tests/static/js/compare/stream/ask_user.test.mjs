// A question or tool approval raised inside one compare pane
// (static/js/compare/stream.js). The agent in a pane can stop on an ask_user
// choice or a tool approval. The card belongs to that pane: it is drawn in the
// pane's history, and the answer goes to that pane's session only, as a
// continuation of the pane rather than a new comparison. If the pane has
// been taken over meanwhile (a reroll, a model swap), the answer is dropped
// instead of landing in the replacement; if the pane never goes idle, the
// card comes back instead of the click vanishing.
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { installDom, waitFor } from '../../_support/dom.mjs';
import { installFetchFake } from '../../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const { default: state } = await import('../../../../../static/js/compare/state.js');
const { streamToPane } = await import('../../../../../static/js/compare/stream.js');

const encoder = new TextEncoder();
let streams;
fake.route('POST', '/api/chat_stream', ({ body }) => {
  let controller;
  const response = new Response(new ReadableStream({ start(c) { controller = c; } }), {
    headers: { 'Content-Type': 'text/event-stream' },
  });
  streams.push({ controller, body });
  return response;
});

const send = (stream, payload) => stream.controller.enqueue(encoder.encode(`data: ${JSON.stringify(payload)}\n\n`));
const close = (stream) => {
  stream.controller.enqueue(encoder.encode('data: [DONE]\n\n'));
  stream.controller.close();
};

function openPane() {
  document.body.innerHTML = '<div id="chat-history"></div>'
    + '<div class="compare-pane" data-pane="0"><div id="cmp-history-0" class="pane-history">'
    + '<div class="msg msg-ai"><div class="body"></div></div></div></div>';
  state._compareMode = 'agent';
  state.isActive = true;
  state._paneSessionIds = ['cmp-1'];
  state._abortControllers = [null];
  const hist = document.getElementById('cmp-history-0');
  const done = streamToPane(0, 'cmp-1', 'compare this', hist.querySelector('.msg-ai'), { timeout: 30 });
  return { hist, done };
}

beforeEach(() => { streams = []; });

const QUESTION = { question: 'Which database?', options: ['Postgres', 'SQLite'] };

test('the question is drawn in the pane that asked and the answer continues that pane\'s session', async () => {
  const { hist, done } = openPane();
  await waitFor(() => streams.length === 1, { what: 'the pane stream' });
  send(streams[0], { type: 'ask_user', data: QUESTION });
  await waitFor(() => hist.querySelector('.ask-user-card'), { what: 'the card in the pane' });
  assert.equal(document.querySelector('#chat-history .ask-user-card'), null);
  assert.equal(hist.querySelector('.ask-user-card').dataset.compareSession, 'cmp-1');
  close(streams[0]);
  await done;

  [...hist.querySelectorAll('.ask-user-option')].find((o) => o.textContent.includes('SQLite')).click();
  await waitFor(() => streams.length === 2, { what: 'the continuation request' });

  assert.equal(streams[1].body.get('session'), 'cmp-1');
  assert.equal(streams[1].body.get('message'), 'SQLite');
  assert.equal(hist.querySelector('.ask-user-card'), null);
  assert.ok([...hist.querySelectorAll('.msg-user .body')].some((b) => b.textContent === 'SQLite'));
  close(streams[1]);
});

test('a tool approval is sent as the approval, with no message and no user bubble', async () => {
  const { hist, done } = openPane();
  await waitFor(() => streams.length === 1, { what: 'the pane stream' });
  send(streams[0], {
    type: 'ask_user',
    data: {
      kind: 'tool_approval',
      approval_id: 'appr-7',
      question: 'Run this command?',
      options: [{ label: 'Allow', value: 'allow' }, { label: 'Deny', value: 'deny' }],
      action: { tool: 'bash', content: 'rm -rf build' },
    },
  });
  await waitFor(() => hist.querySelector('.ask-user-card'), { what: 'the approval card' });
  close(streams[0]);
  await done;

  [...hist.querySelectorAll('.ask-user-option')].find((o) => o.textContent.includes('Deny')).click();
  await waitFor(() => streams.length === 2, { what: 'the approval request' });

  assert.equal(streams[1].body.get('session'), 'cmp-1');
  assert.equal(streams[1].body.get('tool_approval_id'), 'appr-7');
  assert.equal(streams[1].body.get('tool_approval_decision'), 'deny');
  assert.equal(streams[1].body.get('message'), '');
  assert.equal(hist.querySelectorAll('.msg-user').length, 0);
  send(streams[1], { type: 'tool_approval_resolved', decision: 'deny' });
  await waitFor(() => hist.textContent.includes('Denied.'), { what: 'the denial shown in the pane' });
  close(streams[1]);
});

test('an answer to a pane that has since been given another session is not sent', async () => {
  const { hist, done } = openPane();
  await waitFor(() => streams.length === 1, { what: 'the pane stream' });
  send(streams[0], { type: 'ask_user', data: QUESTION });
  await waitFor(() => hist.querySelector('.ask-user-card'), { what: 'the card' });
  close(streams[0]);
  await done;

  state._paneSessionIds = ['cmp-2'];
  hist.querySelector('.ask-user-option').click();
  await new Promise((resolve) => setTimeout(resolve, 80));
  assert.equal(streams.length, 1);
});

test('an answer while the asking stream still owns the pane brings the card back after the wait', async () => {
  const { hist } = openPane();
  await waitFor(() => streams.length === 1, { what: 'the pane stream' });
  send(streams[0], { type: 'ask_user', data: QUESTION });
  await waitFor(() => hist.querySelector('.ask-user-card'), { what: 'the card' });

  const realNow = Date.now;
  try {
    hist.querySelector('.ask-user-option').click();
    assert.equal(hist.querySelector('.ask-user-card'), null);
    const offset = 11000;
    Date.now = () => realNow() + offset;
    await waitFor(() => hist.querySelector('.ask-user-card'), { what: 'the restored card' });
  } finally {
    Date.now = realNow;
  }
  assert.equal(hist.querySelector('.ask-user-card').dataset.comparePane, '0');
  assert.equal(streams.length, 1);
  close(streams[0]);
});
