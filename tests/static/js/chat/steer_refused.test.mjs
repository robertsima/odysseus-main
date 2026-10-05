// A message typed while a turn runs is steered into it first
// (chat/steering.test.mjs). A plain chat reply has no rounds to steer
// between, so the server refuses the steer; static/js/chat.js must then
// queue the message as the next turn instead of dropping it.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { jsonResponse } from '../_support/fetchFake.mjs';
import { openChatPage, waitFor } from './_chatPage.mjs';

const page = await openChatPage();
const steers = [];
page.fake.route('POST', '/api/agents/sessions/s1/steer', ({ body }) => {
  steers.push(JSON.parse(body).text);
  return jsonResponse({ detail: 'This turn cannot be steered' }, { status: 409 });
});

test('a refused steer queues the message as the next turn', async () => {
  const stream = page.streamReply();
  await page.send('Write the changelog');
  stream.event({ delta: 'Writing.' });
  await waitFor(() => page.box.textContent.includes('Writing.'), { what: 'the reply to start' });

  // Enter during a reply (app.js marks the submit as a queued message).
  page.input.value = 'then tag the release';
  window.__odysseusQueueStreamingSubmit = Date.now();
  page.sendButton.click();
  await waitFor(() => page.box.querySelector('.msg-user-queued'), { what: 'the queued bubble' });

  assert.deepEqual(steers, ['then tag the release'], 'it tried the steer first');
  assert.equal(page.box.querySelector('.msg-user-queued .body').textContent, 'then tag the release');
  stream.done();
});
