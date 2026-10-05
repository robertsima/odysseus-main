// A steer the turn ended without reading (static/js/chat.js). The server
// reports it in a steer_dropped event. The page once ignored that event, so a
// typed instruction vanished without a word. It now goes back to the composer
// with an error saying it was not read. A peer agent's message is not the
// user's text and is never put in the composer.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openChatPage, waitFor } from './_chatPage.mjs';

const page = await openChatPage();

async function endTurnWith(dropped) {
  const stream = page.streamReply();
  await page.send('migrate the database');
  stream.event({ delta: 'Working on it.' });
  stream.event({ type: 'steer_dropped', messages: dropped });
  stream.done();
  await page.settled();
}

test('a dropped steer goes back to the composer and the user is told', async () => {
  page.input.value = '';
  await endTurnWith([{ id: 'st1', text: 'also check the archive', kind: 'user' }]);

  await waitFor(() => page.input.value === 'also check the archive', { what: 'the text to return' });
  assert.match(document.getElementById('toast').textContent, /never read it/);
});

test('a peer agent message is not pushed into the composer', async () => {
  page.input.value = '';
  await endTurnWith([{ id: 'st2', text: 'build finished', kind: 'peer' }]);

  assert.equal(page.input.value, '');
});
