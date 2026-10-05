// An MCP server's connection (transport, command, args, env, URL) can be
// changed after it was added: the editor in Settings > Integrations sends the
// new values to PUT /api/mcp/servers/{id}, which saves them and reconnects.
// Before that route existed the only way to change a command or URL was to
// delete the server and add it again, losing its tool choices.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const SERVER = {
  id: 'srv1', name: 'files', transport: 'stdio', command: 'npx', args: ['-y', 'old-server'],
  env: { TOKEN: 'old' }, url: null, is_enabled: true, status: 'connected', tool_count: 3, enabled_tool_count: 3,
};
fake.route('GET', '/api/auth/integrations', () => ({ integrations: [] }));
fake.route('GET', '/api/calendar/config/accounts', () => ({ accounts: [] }));
fake.route('GET', '/api/contacts/config', () => ({}));
fake.route('GET', '/api/contacts/list', () => ({ contacts: [], count: 0 }));
fake.route('GET', '/api/email/accounts', () => ({ accounts: [] }));
fake.route('GET', '/api/mcp/servers', () => [SERVER]);
fake.route('GET', '/api/tokens', () => []);
fake.route('PUT', '/api/mcp/servers/srv1', () => ({ ok: true, connected: true, tool_count: 3 }));

document.body.innerHTML = `
  <div><button id="unified-intg-add-btn" type="button">Add</button></div>
  <div id="unified-integrations-list"></div>
  <div id="unified-intg-form" style="display:none"></div>`;

const { default: settingsModule } = await import('../../../../static/js/settings.js');
const byId = (id) => document.getElementById(id);

test('saving connection settings sends the new transport, URL and env to the server', async () => {
  await settingsModule.initUnifiedIntegrations();
  document.querySelector('.intg-card[data-intg-id="srv1"]').click();
  await waitFor(() => byId('uf-mcp-save-conn'), { what: 'the MCP editor' });

  byId('uf-mcp-edit-transport').value = 'http';
  byId('uf-mcp-edit-transport').dispatchEvent(new Event('change'));
  byId('uf-mcp-edit-url').value = 'http://mcp.local/mcp';
  byId('uf-mcp-edit-env').value = '{"TOKEN": "new"}';
  byId('uf-mcp-save-conn').click();

  const saves = () => fake.calls.filter((c) => c.url.pathname === '/api/mcp/servers/srv1' && c.method !== 'GET');
  await waitFor(() => saves().length, { what: 'the save' });
  const [save] = saves();
  assert.equal(save.method, 'PUT');
  assert.equal(save.body.get('name'), 'files');
  assert.equal(save.body.get('transport'), 'http');
  assert.equal(save.body.get('url'), 'http://mcp.local/mcp');
  assert.deepEqual(JSON.parse(save.body.get('env')), { TOKEN: 'new' });
  assert.deepEqual(JSON.parse(save.body.get('args')), ['-y', 'old-server']);
  await waitFor(() => /Saved\. Connected/.test(byId('uf-mcp-conn-msg').textContent), { what: 'the saved message' });
});
