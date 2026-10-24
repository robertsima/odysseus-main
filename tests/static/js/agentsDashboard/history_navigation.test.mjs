// History has one unambiguous continuation per saved conversation, not a
// profile-level Talk action that can open a different conversation.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { openRoom, waitFor } from './_room.mjs';

const room = await openRoom({ historyChats: [
  { id: 'old-chat', name: 'Older Reader chat', profile: 'Reader', message_count: 2 },
] });

test('history continues the exact saved chat without a competing profile resume', async () => {
  const opened = [];
  window.sessionModule.selectSession = async (sid) => opened.push(sid);
  room.dashboard.open();
  room.click('[data-ag="history-view"]');
  await waitFor(() => room.root.querySelector('.ag-history-item'), { what: 'saved conversation' });
  const item = room.root.querySelector('.ag-history-item');
  assert.ok(!item.querySelector('[data-agent-act="talk"]'), 'history must not offer a competing profile-level chat');
  assert.ok(item.querySelector('[data-agent-act="assign"]'), 'task assignment remains available');
  item.querySelector('[data-ag="open-chat"]').click();
  await waitFor(() => opened.length, { what: 'saved conversation opened' });
  assert.deepEqual(opened, ['old-chat']);
  assert.ok(!room.fake.calls.some(c => c.method === 'POST' && c.url.pathname.endsWith('/chat')));
  room.dashboard.close();
});
