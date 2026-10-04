// The model name on a reply bubble, in static/js/chat.js. Model names come
// from whatever an endpoint's /v1/models lists, so a name carrying markup must
// be shown as text, never parsed as HTML, on every path that draws a reply:
// a fresh send, a run the page reattaches to, and a research run it reconnects
// to.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { jsonResponse } from '../_support/fetchFake.mjs';
import { openChatPage, waitFor } from './_chatPage.mjs';

const MODEL = 'evil<img src=x onerror=alert(1)>';
const page = await openChatPage({ model: MODEL });
page.fake.route('GET', '/api/sessions', () => [
  { id: 's1', name: 'chat', model: MODEL, endpoint_url: 'http://127.0.0.1:9/v1', archived: false },
]);
await page.sessionModule.loadSessions();

function lastRole(selector = '.msg-ai') {
  const bubbles = page.box.querySelectorAll(selector);
  return bubbles[bubbles.length - 1].querySelector('.role');
}

function assertShownAsText(role) {
  assert.equal(role.querySelectorAll('img').length, 0, role.innerHTML);
  assert.match(role.textContent, /^evil<img src=x onerror/);
}

test('a new reply shows a model name with markup as text', async () => {
  const stream = page.streamReply();
  await page.send('hello');
  await waitFor(() => page.box.querySelector('.msg-ai'), { what: 'the reply bubble' });
  assertShownAsText(lastRole());
  stream.event({ delta: 'Hi.' });
  stream.done();
  await page.settled();
});

test('a reattached run shows a model name with markup as text', async () => {
  const encoder = new TextEncoder();
  let run;
  page.fake.route('GET', '/api/chat/resume/s1', () => new Response(new ReadableStream({
    start(controller) { run = controller; },
  }), { headers: { 'Content-Type': 'text/event-stream' } }));

  const resumed = page.chatModule.resumeStream('s1');
  await waitFor(() => page.box.querySelector('.msg-ai .stream-content'), { what: 'the reattached bubble' });
  assertShownAsText(page.box.querySelector('.msg-ai .stream-content').closest('.msg-ai').querySelector('.role'));

  run.enqueue(encoder.encode('data: [DONE]\n\n'));
  run.close();
  await resumed;
});

test('a research reconnect shows a model name with markup as text', async () => {
  page.fake.route('GET', '/api/research/status/s1', () => jsonResponse({ status: 'running' }));

  await page.chatModule.checkPendingResearch('s1');

  assertShownAsText(lastRole('.research-reconnect'));
});
