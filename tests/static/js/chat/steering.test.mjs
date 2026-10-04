// Steering a running turn, in static/js/chat.js. A message sent while an agent
// reply streams goes to POST /api/agents/sessions/{id}/steer and shows as a
// pending bubble. When the agent reads it (the stream's steer_applied event,
// or the lifecycle poll) the bubble joins the transcript as an ordinary user
// message; when it fails, the bubble goes and the user is told. A history
// redraw can remove the pending bubble; an injected steer then comes back once,
// with its full text. A steer answer that arrives after the user opened
// another chat must not draw anything there.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openChatPage, waitFor } from './_chatPage.mjs';

const page = await openChatPage();
let nextSteer = 0;
let steerReply = null; // a test can hold the steer POST open
page.fake.route('POST', '/api/agents/sessions/s1/steer', async () => {
  if (steerReply) await steerReply;
  return { id: `st${++nextSteer}` };
});
let poll = { messages: [] };
page.fake.route('GET', '/api/agents/sessions/s1/steer', () => poll);
page.fake.route('GET', '/api/history/s2', () => ({ history: [], model: 'test-model', total: 0 }));
page.fake.route('GET', '/api/sessions', () => [{ id: 's1', name: 'one' }, { id: 's2', name: 'two' }]);

// Headers of each lifecycle poll, which the fetch fake does not record.
const pollHeaders = [];
const fakeFetch = globalThis.fetch;
globalThis.fetch = (input, init = {}) => {
  if (String(input).includes('/steer?')) pollHeaders.push(init.headers || {});
  return fakeFetch(input, init);
};

// The lifecycle poll runs every 1.2 s; give it a few rounds on a slow runner.
const POLL_WAIT = 6000;

function steerBubble(id) {
  return page.box.querySelector(`.msg-user[data-steer-id="${id}"]`);
}

function toastText() {
  return document.getElementById('toast').textContent;
}

// Enter in the composer during a reply marks the submit as a queued message
// (app.js) and submits the form.
function pressEnterWhileStreaming(text) {
  page.input.value = text;
  window.__odysseusQueueStreamingSubmit = Date.now();
  page.sendButton.click();
}

async function startTurn() {
  const stream = page.streamReply();
  await page.send('migrate the database');
  stream.event({ delta: 'Working on it.' });
  await waitFor(() => page.box.textContent.includes('Working on it.'), { what: 'the reply to start' });
  return stream;
}

async function steer(text) {
  const before = nextSteer;
  pressEnterWhileStreaming(text);
  await waitFor(() => nextSteer > before, { what: 'the steer POST' });
  const id = `st${nextSteer}`;
  await waitFor(() => steerBubble(id), { what: 'the pending steer bubble' });
  return id;
}

async function endTurn(stream) {
  stream.event({ delta: ' Done.' });
  stream.done();
  await page.settled();
}

test('a steer read by the agent joins the transcript as a user message', async () => {
  poll = { messages: [] };
  const stream = await startTurn();
  const id = await steer('focus on the users table');
  assert.ok(steerBubble(id).classList.contains('msg-user-steered'));

  stream.event({ type: 'steer_applied', steer_id: id, round: 2 });
  await waitFor(() => !steerBubble(id).classList.contains('msg-user-steered'), { what: 'the promotion', timeout: POLL_WAIT });

  const bubble = steerBubble(id);
  assert.equal(bubble.parentNode, page.box);
  assert.equal(bubble.querySelector('.role').textContent, 'You');
  await endTurn(stream);
});

test('the lifecycle poll promotes an injected steer and marks itself as a poll', async () => {
  const stream = await startTurn();
  poll = { messages: [] };
  const id = await steer('also drop the temp tables');
  poll = { messages: [{ id, state: 'injected' }] };

  await waitFor(() => !steerBubble(id).classList.contains('msg-user-steered'), { what: 'the promotion', timeout: POLL_WAIT });

  assert.ok(pollHeaders.length > 0);
  assert.ok(pollHeaders.every((h) => h['X-Odysseus-Poll'] === '1'), JSON.stringify(pollHeaders));
  await endTurn(stream);
});

test('a failed steer leaves the page and says it failed', async () => {
  const stream = await startTurn();
  poll = { messages: [] };
  const id = await steer('rename the column');
  poll = { messages: [{ id, state: 'failed', reason: 'turn ended' }] };

  await waitFor(() => !steerBubble(id), { what: 'the failed bubble to go', timeout: POLL_WAIT });

  assert.match(toastText(), /Steering failed: turn ended/);
  await endTurn(stream);
});

test('an injected steer whose bubble a redraw removed comes back once, with its full text', async () => {
  const stream = await startTurn();
  poll = { messages: [] };
  const text = 'complete instruction '.repeat(60).trim();
  const id = await steer(text);

  // What a history redraw does to the pending bubble.
  page.box.querySelectorAll('.msg-user-steered').forEach((node) => node.closest('.chat-queued-bubble-host')?.remove());
  assert.equal(steerBubble(id), null);
  poll = { messages: [{ id, state: 'injected', text: text.slice(0, 40) }] };

  await waitFor(() => steerBubble(id), { what: 'the restored bubble', timeout: POLL_WAIT });
  const restored = page.box.querySelectorAll(`.msg-user[data-steer-id="${id}"]`);
  assert.equal(restored.length, 1);
  assert.equal(restored[0].dataset.raw, text);
  assert.equal(restored[0].querySelector('.role').textContent, 'You');
  await endTurn(stream);
});

test('a steer answer that arrives after the user opened another chat draws nothing there', async () => {
  const stream = await startTurn();
  let release;
  steerReply = new Promise((resolve) => { release = resolve; });
  const before = nextSteer;
  pressEnterWhileStreaming('focus on the migration');

  await page.sessionModule.selectSession('s2', { showLoading: false });
  release();
  steerReply = null;
  await waitFor(() => nextSteer > before, { what: 'the steer answer' });
  await new Promise((resolve) => setTimeout(resolve, 50));

  assert.equal(page.box.querySelectorAll('.msg-user-steered, .chat-queued-bubble').length, 0);
  assert.equal(page.input.value, '');
  stream.done();
});
