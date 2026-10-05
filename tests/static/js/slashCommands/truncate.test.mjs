// "/chats truncate N" in static/js/slashCommands.js deletes older messages and
// keeps the newest N, as its usage line says. It used to send N as a count to
// keep from the start, which kept the oldest N and deleted the recent ones.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openChatPage, waitFor } from '../chat/_chatPage.mjs';

const page = await openChatPage();
const truncates = [];
page.fake.route('POST', '/api/session/s1/truncate', ({ body }) => {
  truncates.push(JSON.parse(body));
  return { ok: true };
});

test('/chats truncate 3 asks the server to keep the last 3 messages', async () => {
  page.input.value = '/chats truncate 3';
  page.sendButton.click();
  await waitFor(() => truncates.length === 1, { what: 'the truncate request' });

  assert.deepEqual(truncates, [{ keep_last: 3 }]);
  assert.deepEqual(page.posts, [], 'a slash command never reaches the model');
});
