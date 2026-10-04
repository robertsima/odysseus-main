// Every save in the Settings window goes through one writer that drops the
// shared /api/auth/settings snapshot (static/js/appConfig.js). Without that,
// other modules keep reading the boot snapshot for the rest of the session.
// Driven through the chat display "fold after" input, one of those saves.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const index = new DOMParser().parseFromString(
  readFileSync(new URL('../../../../static/index.html', import.meta.url), 'utf8'), 'text/html');
document.body.appendChild(document.importNode(index.getElementById('settings-modal'), true));

const server = { chat_tool_fold_after: 12 };
const saves = [];
fake.route('GET', '/api/auth/settings', () => ({ ...server }));
fake.route('POST', '/api/auth/settings', ({ body }) => {
  saves.push(JSON.parse(body));
  Object.assign(server, JSON.parse(body));
  return { ...server };
});

const { getSettings } = await import('../../../../static/js/appConfig.js');
const settings = await import('../../../../static/js/settings.js');

test('a saved setting reaches the next shared settings read', async () => {
  assert.equal((await getSettings()).chat_tool_fold_after, 12);

  settings.open('appearance');
  const input = document.getElementById('set-chatFoldAfter');
  await waitFor(() => input.value === '12', { what: 'the panel to load its value' });
  input.value = '30';
  input.dispatchEvent(new Event('change'));
  await waitFor(() => saves.length > 0, { what: 'the settings POST' });
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.equal((await getSettings()).chat_tool_fold_after, 30);
});
