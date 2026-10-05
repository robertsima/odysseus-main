// Regenerate and fork name messages by their stored id, in static/js/chat.js.
// A chat opens on its newest page, so a position counted on screen is not a
// position in the stored chat: truncating or forking "after N messages" cut
// the wrong turn whenever older history was not loaded. The ids come from the
// history page and, for a turn that just streamed, from the stream's
// user_message_saved and message_saved events.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openChatPage, waitFor } from './_chatPage.mjs';

const HISTORY = [
  { role: 'user', content: 'list three rivers', metadata: { _db_id: 'u40' } },
  { role: 'assistant', content: 'Nile, Amazon, Danube', metadata: { _db_id: 'a41' } },
];
const page = await openChatPage({ history: HISTORY });
const requests = [];
page.fake.route('POST', '/api/session/s1/truncate', ({ body }) => {
  requests.push({ truncate: JSON.parse(body) });
  return { ok: true };
});
page.fake.route('POST', '/api/session/s1/fork', ({ body }) => {
  requests.push({ fork: JSON.parse(body) });
  return { id: 's2', name: 'fork' };
});
page.fake.route('GET', '/api/sessions', () => [{ id: 's1', name: 'chat' }, { id: 's2', name: 'fork' }]);
page.fake.route('GET', '/api/history/s2', () => ({ history: HISTORY, model: 'test-model', total: 2 }));

test('regenerating a reply that just streamed truncates from the saved id of its question', async () => {
  const first = page.streamReply();
  await page.send('and three mountains');
  first.event({ type: 'user_message_saved', id: 'u42' });
  first.event({ delta: 'Everest, K2, Denali' });
  first.event({ type: 'message_saved', id: 'a43' });
  first.done();
  await page.settled();

  const replies = page.box.querySelectorAll('.msg-ai');
  const again = page.streamReply();
  page.clickAction(replies[replies.length - 1], 'Regenerate from here');
  await waitFor(() => requests.length === 1, { what: 'the truncate' });
  again.event({ delta: 'Everest, Aconcagua, Kilimanjaro' });
  again.done();
  await page.settled();

  assert.deepEqual(structuredClone(requests), [{ truncate: { from_message_id: 'u42' } }]);
});

test('forking from a reply names that reply by its id', async () => {
  requests.length = 0;
  page.clickAction(page.box.querySelector('.msg[data-db-id="a41"]'), 'Fork conversation');
  await waitFor(() => page.sessionModule.getCurrentSessionId() === 's2', { what: 'the fork to open' });

  assert.deepEqual(structuredClone(requests), [{ fork: { through_message_id: 'a41' } }]);
});
