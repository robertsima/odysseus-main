// "New chat" in static/app.js starts the new chat on the model the user is
// already working with: the open chat's model, with the configured default as
// the fallback. Starting from the default instead quietly moved people off
// the model they had chosen, and the rail, sidebar and logo buttons each had
// their own copy of the choice, so they disagreed. (A model picked for a chat
// not yet sent is kept twice over, here and in sessions.createDirectChat, so
// neither copy alone shows in behavior.)
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { openAppPage, waitFor } from './_appPage.mjs';

const OPEN_CHAT = { id: 's1', name: 'Open chat', endpoint_url: 'http://current.test/v1/chat/completions',
  model: 'current-model', endpoint_id: 'ep-current', archived: false };
const DEFAULT = { endpoint_url: 'http://default.test/v1/chat/completions', model: 'default-model', endpoint_id: 'ep-default' };

const page = await openAppPage({
  routes(fake) {
    fake.route('GET', '/api/sessions', () => [OPEN_CHAT]);
    fake.route('GET', '/api/default-chat', () => DEFAULT);
    fake.route('GET', '/api/history/s1', () => ({ history: [], model: 'current-model', total: 0 }));
  },
});

async function openChat() {
  await page.sessionModule.selectSession('s1', { showLoading: false });
  assert.equal(page.sessionModule.getCurrentSessionId(), 's1');
  assert.equal(page.sessionModule.hasPendingChat(), false);
}

async function clickNewChat(id) {
  document.getElementById(id).click();
  await waitFor(() => page.sessionModule.getPendingChat?.(), { what: 'the new chat' });
  // The handler may still be fetching the default chat; let it finish.
  await new Promise((resolve) => setTimeout(resolve, 100));
  return page.sessionModule.getPendingChat();
}

for (const button of ['rail-new-session', 'sidebar-brand-btn', 'sidebar-new-chat-btn']) {
  test(`#${button} starts the new chat on the open chat's model, not the default`, async () => {
    await openChat();

    const pending = await clickNewChat(button);

    assert.equal(pending.modelId, 'current-model');
    assert.equal(pending.endpointId, 'ep-current');
  });
}
