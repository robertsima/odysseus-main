// Deleting a message that was never saved, in static/js/chat.js. Before a
// model is picked there is no chat session, but the page can still show a
// reply (the "no chat session active" help, an error). Its delete button has
// to remove it; it used to do nothing because the delete returned early
// without a session (#1428).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openChatPage, waitFor } from './_chatPage.mjs';

const page = await openChatPage({ sessionId: null });

test('deleting a reply with no chat session open removes it from the page', async () => {
  const reply = page.chatModule.addMessage('assistant', 'No chat session active.');
  assert.equal(page.sessionModule.getCurrentSessionId(), null);

  reply.querySelector('.msg-delete-btn').click();
  await waitFor(() => document.getElementById('styled-confirm-ok'), { what: 'the confirm dialog' });
  document.getElementById('styled-confirm-ok').click();

  await waitFor(() => !reply.isConnected, { what: 'the reply to be removed' });
  assert.equal(page.box.querySelectorAll('.msg').length, 0);
  assert.deepEqual(page.fake.calls.filter((c) => c.url.pathname.includes('delete-messages')), []);
});
