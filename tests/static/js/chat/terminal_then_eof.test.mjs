// A stream that ends right after the server's canonical terminal event
// (static/js/chat.js). The terminal marker means the turn is saved, so the
// early close must not reattach to the run as if the connection had died; the
// chat loads the saved record. A stream that closes with no terminal marker is
// a dropped connection and still reattaches.
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { openChatPage } from './_page.mjs';

const page = await openChatPage({ mode: 'agent' });
after(() => page.close());

const count = (method, path) => page.fake.calls.filter((c) => c.method === method && c.url.pathname === path).length;
page.fake.route('GET', '/api/chat/resume/s1', () => new Response(null, { status: 204 }));

beforeEach(() => {
  page.posts.length = 0;
  page.fake.calls.length = 0;
  page.history.s1 = [];
});

test('a close after the terminal event loads the saved record and does not reattach', async () => {
  page.history.s1 = [
    { role: 'user', content: 'summarise it' },
    { role: 'assistant', content: 'Saved answer from the server' },
  ];
  page.reply([{ delta: 'Half of the answer' }, { type: 'agent_terminal', data: { model: 'model-a' } }]);

  await page.send('summarise it');
  await page.waitFor(() => page.chatBox().textContent.includes('Saved answer from the server'), {
    what: 'the saved record',
  });

  assert.equal(count('GET', '/api/chat/resume/s1'), 0);
  assert.equal(page.posts.length, 1);
});

test('a close with no terminal event reattaches to the running turn', async () => {
  page.reply([{ delta: 'Half of the answer' }]);

  await page.send('summarise it');
  await page.waitFor(() => count('GET', '/api/chat/resume/s1') > 0, { what: 'the reattach' });

  assert.equal(page.posts.length, 1, 'the message was posted again');
});
