// Stopping a reply before its response headers arrive (static/js/chat.js).
//
// The server run is detached and named by the X-Odysseus-Run-Id response
// header. A Stop pressed (or a timeout hit) before that header has no safe
// identity to send: a headerless session-wide cancel could hit a newer run. So
// the Stop is queued and fires with the exact run id once the header arrives;
// a POST that never gets headers is hard-aborted after a short grace.
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { openChatPage, sseResponse } from './_page.mjs';

const page = await openChatPage();
after(() => page.close());

// Every fetch with its headers and abort signal; the page fake keeps neither.
const requests = [];
const realFetch = globalThis.fetch;
globalThis.fetch = (input, init = {}) => {
  requests.push({ url: String(input), method: init.method || 'GET', headers: init.headers || {}, signal: init.signal });
  return realFetch(input, init);
};

// Long timers (the reply timeout and the abort grace) are held for the test to
// fire by hand; everything else runs normally.
const realSetTimeout = globalThis.setTimeout;
let heldTimers = [];
globalThis.setTimeout = (callback, delay, ...rest) => {
  if (delay >= 1500) {
    heldTimers.push({ callback, delay });
    return heldTimers.length;
  }
  return realSetTimeout(callback, delay, ...rest);
};
after(() => { globalThis.setTimeout = realSetTimeout; });

const stopRequests = () => requests.filter((r) => r.url === '/api/chat/stop/s1');
const chatPost = () => requests.find((r) => r.url.endsWith('/api/chat_stream'));

// A POST whose response headers the test releases later.
function gatedReply() {
  let open;
  const gate = new Promise((resolve) => { open = resolve; });
  page.reply(() => gate);
  return {
    headers(runId) {
      const response = sseResponse([{ delta: 'hello' }], { runId, hold: true });
      open(response);
      return response;
    },
  };
}

beforeEach(() => {
  requests.length = 0;
  heldTimers = [];
  page.posts.length = 0;
});

async function startSend(text) {
  const reply = gatedReply();
  const sending = page.send(text).catch(() => {});
  await page.waitFor(() => chatPost(), { what: 'the chat_stream POST' });
  return { reply, sending };
}

test('Stop before the headers arrive sends nothing, then fires with the exact run id', async () => {
  const { reply, sending } = await startSend('first');

  page.chat.abortCurrentRequest(true);
  await new Promise((resolve) => realSetTimeout(resolve, 20));
  assert.deepEqual(stopRequests(), [], 'a headerless Stop went out');
  assert.equal(chatPost().signal.aborted, false, 'the POST was aborted before it could name its run');

  const response = reply.headers('run-9');
  await page.waitFor(() => stopRequests().length === 1, { what: 'the exact Stop' });
  assert.equal(stopRequests()[0].method, 'POST');
  assert.equal(stopRequests()[0].headers['X-Agamemnon-Run-Id'], 'run-9');
  await page.waitFor(() => chatPost().signal.aborted, { what: 'the reader to abort' });
  response.release();
  await sending;
});

test('the reply timeout before the headers arrive also waits for the run id', async () => {
  const { reply, sending } = await startSend('second');
  const timeout = heldTimers.find((t) => t.delay >= 60000);
  assert.ok(timeout, 'the reply timeout was not armed');

  timeout.callback();
  assert.equal(chatPost().signal.aborted, false);
  assert.deepEqual(stopRequests(), []);

  const response = reply.headers('run-timeout');
  await page.waitFor(() => stopRequests().length === 1, { what: 'the exact Stop' });
  assert.equal(stopRequests()[0].headers['X-Agamemnon-Run-Id'], 'run-timeout');
  assert.equal(chatPost().signal.aborted, true);
  response.release();
  await sending;
});

test('a POST that never gets headers is hard-aborted after the timeout grace', async () => {
  const { reply, sending } = await startSend('third');
  const timeout = heldTimers.find((t) => t.delay >= 60000);

  timeout.callback();
  assert.equal(chatPost().signal.aborted, false);
  const grace = heldTimers.find((t) => t.delay === 2000);
  assert.ok(grace, 'no abort grace was armed');
  grace.callback();

  assert.equal(chatPost().signal.aborted, true);
  assert.deepEqual(stopRequests(), [], 'no run id was ever seen, so no Stop is sent');
  reply.headers('late').release(); // let the aborted send unwind
  await sending;
});
