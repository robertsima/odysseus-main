// Switching chats in the gap between a turn's canonical agent_terminal event
// and the SSE error frame that follows it (static/js/chat.js). The server has
// already saved the turn, so the outgoing chat is complete. Marking it as
// still streaming leaves a pulsing dot on it that nothing clears.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { openChatPage, sseResponse } from './_page.mjs';

const page = await openChatPage({ mode: 'agent' });
after(() => page.close());

test('switching chats after the terminal event does not mark the chat as streaming', async () => {
  const marked = [];
  const cleared = [];
  page.sessionModule.markStreaming = (id) => marked.push(id);
  page.sessionModule.clearStreaming = (id) => cleared.push(id);
  const held = sseResponse([
    { delta: 'Half of the answer' },
    { type: 'agent_terminal', data: { model: 'model-a' } },
  ], { hold: true });
  page.reply(held);

  const sending = page.send('answer in full');
  await page.waitFor(() => page.chatBox().textContent.includes('Half of the answer'), { what: 'the partial reply' });
  // Let the reader take the terminal frame that follows the delta.
  await new Promise((resolve) => setTimeout(resolve, 100));
  await page.sessionModule.selectSession('s2', { showLoading: false });

  assert.deepEqual(marked, []);
  assert.deepEqual(cleared, ['s1']);
  held.release();
  await sending.catch(() => {});
});
