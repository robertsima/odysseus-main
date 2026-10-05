// An ask_user event in a live reply (static/js/chat.js) ends the turn on a
// multiple-choice question. The question must show as a choice card in the
// transcript, or the user sees a turn that stopped for no visible reason.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openChatPage, waitFor } from './_chatPage.mjs';

const page = await openChatPage();

test('a live ask_user event shows its choice card', async () => {
  const stream = page.streamReply();
  await page.send('Plan the migration');
  stream.event({ type: 'ask_user', data: { question: 'Which database?', options: ['Postgres', 'SQLite'] } });
  await waitFor(() => page.box.querySelector('.ask-user-card'), { what: 'the choice card' });
  stream.done();
  await page.settled();

  const card = page.box.querySelector('.ask-user-card');
  assert.equal(card.querySelector('.ask-user-question').textContent, 'Which database?');
  assert.deepEqual([...card.querySelectorAll('.ask-user-option-label')].map((o) => o.textContent), ['Postgres', 'SQLite']);
});
