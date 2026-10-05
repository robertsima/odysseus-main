// Resending a user message, in static/js/chat.js and the message footer of
// static/js/chatRenderer.js. "Resend message" sends the same text again as a
// new turn and leaves the conversation alone; it once truncated everything
// after the message on the server and on screen (#4149). Only a regenerate,
// such as "Regenerate message" after correcting a photo's caption, replaces
// the messages after it.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openChatPage, waitFor } from './_chatPage.mjs';

const HISTORY = [
  { role: 'user', content: 'draft a haiku about rain', metadata: { _db_id: 'u1' } },
  { role: 'assistant', content: 'Soft rain on the roof', metadata: { _db_id: 'a1' } },
  { role: 'user', content: 'now one about this photo',
    metadata: { _db_id: 'u2', attachments: [{ id: 'f1', name: 'photo.png', mime: 'image/png' }] } },
  { role: 'assistant', content: 'White hush on the field', metadata: { _db_id: 'a2' } },
];
const page = await openChatPage({ history: HISTORY });
const truncates = [];
page.fake.route('POST', '/api/session/s1/truncate', ({ body }) => {
  truncates.push(JSON.parse(body));
  return { ok: true };
});

page.fake.route('GET', '/api/upload/f1/vision', () => ({ text: 'a dog in the snow' }));
page.fake.route('PUT', '/api/upload/f1/vision', () => ({ ok: true }));

function bubble(dbId) {
  return page.box.querySelector(`.msg[data-db-id="${dbId}"]`);
}

test('Resend message sends the text again and keeps every message', async () => {
  const before = page.posts.length;
  const stream = page.streamReply();
  page.clickAction(bubble('u1'), 'Resend message');
  await waitFor(() => page.posts.length > before, { what: 'the resend' });
  stream.event({ delta: 'Rain again' });
  stream.done();
  await page.settled();

  assert.equal(page.posts[page.posts.length - 1].get('message'), 'draft a haiku about rain');
  assert.deepEqual(truncates, []);
  for (const id of ['u1', 'a1', 'u2', 'a2']) assert.ok(bubble(id), `message ${id} is still shown`);
});

test('Regenerate message from a photo caption edit replaces the reply after it', async () => {
  bubble('u2').querySelector('.attach-ocr-btn').click();
  const regenerate = () => document.querySelector('.vision-editor-overlay button[title="Save and regenerate the message"]');
  await waitFor(() => regenerate() && !regenerate().disabled, { what: 'the caption editor' });
  document.querySelector('.vision-editor-text').value = 'a cat in the snow';

  const stream = page.streamReply();
  regenerate().click();
  await waitFor(() => truncates.length === 1, { what: 'the truncate' });
  stream.event({ delta: 'A cat in the snow.' });
  stream.done();
  await page.settled();

  assert.deepEqual(truncates, [{ from_message_id: 'u2' }]);
  assert.equal(bubble('a2'), null, 'the reply after it is replaced');
  assert.ok(bubble('a1'), 'earlier messages stay');
});
