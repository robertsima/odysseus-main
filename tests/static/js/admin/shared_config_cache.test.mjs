// Admin writers and the shared /api/tools and /api/auth/settings snapshots in
// static/js/appConfig.js. The Tools editor posts the whole disabled list, so
// it must build that list from the server's current state: a stale snapshot
// would re-enable a tool someone disabled elsewhere. After any write, the next
// shared read must see the new value instead of the boot snapshot.
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

// The server: which tools are enabled, and the settings object.
const server = {
  tools: { web_search: true, shell: true },
  settings: { share_defaults_with_users: false },
};
const posts = [];
fake.route('GET', '/api/tools', () => ({
  tools: Object.entries(server.tools).map(([id, enabled]) => ({ id, enabled })),
}));
fake.route('POST', '/api/tools', ({ body }) => {
  const { disabled } = JSON.parse(body);
  posts.push(disabled);
  for (const id of Object.keys(server.tools)) server.tools[id] = !disabled.includes(id);
  return { ok: true };
});
fake.route('GET', '/api/auth/settings', () => ({ ...server.settings }));
fake.route('POST', '/api/auth/settings', ({ body }) => {
  Object.assign(server.settings, JSON.parse(body));
  return { ...server.settings };
});

const { getSettings, getTools } = await import('../../../../static/js/appConfig.js');
const admin = await import('../../../../static/js/admin.js');

const checkbox = (id) => document.querySelector(`#adm-builtin-tools-list input[data-tool-id="${id}"]`);

async function toggle(input, checked) {
  const before = posts.length;
  input.checked = checked;
  input.dispatchEvent(new Event('change'));
  await waitFor(() => posts.length > before, { what: 'the tools POST' });
  await new Promise((resolve) => setTimeout(resolve, 0));
}

test('the Tools editor shows the server state, not the snapshot taken at boot', async () => {
  // Boot: chatRenderer.js reads the shared tool list while both are enabled.
  assert.equal((await getTools()).tools.find((t) => t.id === 'web_search').enabled, true);
  // Another tab (or the manage_settings tool) disables web_search.
  server.tools.web_search = false;

  admin._initData('agents');
  await waitFor(() => checkbox('web_search'), { what: 'the tool list' });
  assert.equal(checkbox('web_search').checked, false);
});

test('a tool toggle keeps a change made elsewhere after the editor opened', async () => {
  server.tools.web_search = true;
  admin._initData('agents');
  await waitFor(() => checkbox('web_search')?.checked === true, { what: 'the refreshed tool list' });

  // web_search is disabled elsewhere while this editor still shows it on.
  server.tools.web_search = false;
  await toggle(checkbox('shell'), false);

  assert.deepEqual(posts.at(-1).sort(), ['shell', 'web_search']);
});

test('after a tool toggle the shared tool list reads the saved state', async () => {
  await toggle(checkbox('shell'), true);
  const shared = await getTools();
  assert.equal(shared.tools.find((t) => t.id === 'shell').enabled, true);
});

test('after the share-defaults toggle the shared settings read the saved value', async () => {
  admin._initData('users');
  assert.equal((await getSettings()).share_defaults_with_users, false);

  const share = document.getElementById('adm-shareDefaultsToggle');
  share.checked = true;
  share.dispatchEvent(new Event('change'));
  await waitFor(() => server.settings.share_defaults_with_users === true, { what: 'the settings POST' });
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.equal((await getSettings()).share_defaults_with_users, true);
});
