// A turn that ends on agent_terminal (static/js/chat.js): the server has
// saved the record and sends the turn's usage with the terminal event. The
// live stream and a stream resumed after a reload must both record that usage
// in the session cost ledger under the run's id, or the cost of a run that
// ended this way is never counted.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openChatPage, waitFor } from './_chatPage.mjs';

const page = await openChatPage();
// app.js publishes the session module; the cost ledger reads it from window.
window.sessionModule = page.sessionModule;

// gpt-4o is priced at $2.50 in and $10 out per million tokens
// (MODEL_INFO in static/js/chatRenderer.js).
const USAGE = { model: 'gpt-4o', input_tokens: 1000, output_tokens: 100, endpoint_cost_tracked: true, response_time: 2 };
const COST = (1000 * 2.5 + 100 * 10) / 1e6;

function ledger() {
  return JSON.parse(localStorage.getItem('ody-session-cost-runs') || '{}').s1 || {};
}

function assertRecorded(runId) {
  assert.ok(Math.abs((ledger()[`${runId}:primary`] ?? NaN) - COST) < 1e-12, JSON.stringify(ledger()));
}

test('a live turn that ends on agent_terminal records its usage', async () => {
  const stream = page.streamReply();
  stream.response.headers.set('X-Odysseus-Run-Id', 'run-live');
  await page.send('Summarise the report');
  stream.event({ delta: 'Partial answer' });
  stream.event({ type: 'agent_terminal', data: { ...USAGE } });
  stream.done();
  await page.settled();

  await waitFor(() => ledger()['run-live:primary'] !== undefined, { what: 'the ledger write' }).catch(() => {});
  assertRecorded('run-live');
});

test('a resumed turn that ends on agent_terminal records its usage', async () => {
  const encoder = new TextEncoder();
  let replay;
  const body = new ReadableStream({ start(c) { replay = c; } });
  page.fake.route('GET', '/api/chat/resume/s1', () => new Response(body, {
    headers: { 'Content-Type': 'text/event-stream', 'X-Odysseus-Run-Id': 'run-resumed' },
  }));
  const event = (payload) => replay.enqueue(encoder.encode(`data: ${JSON.stringify(payload)}\n\n`));

  const done = page.chatModule.resumeStream('s1');
  await waitFor(() => replay, { what: 'the resume request' });
  event({ delta: 'Partial answer' });
  event({ type: 'agent_terminal', data: { ...USAGE } });
  replay.enqueue(encoder.encode('data: [DONE]\n\n'));
  replay.close();
  assert.equal(await done, true);

  await waitFor(() => ledger()['run-resumed:primary'] !== undefined, { what: 'the ledger write' }).catch(() => {});
  assertRecorded('run-resumed');
});
