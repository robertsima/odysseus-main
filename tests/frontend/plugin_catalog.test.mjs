import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../../static/js/pluginCatalog.js', import.meta.url), 'utf8');
function fixture() {
  const nodes = new Map();
  const calls = [];
  const window = { confirm: () => false, dispatchEvent() {} };
  const document = { createElement() { return {
    textContent: '', get innerHTML() {
      return this.textContent.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    },
  }; } };
  vm.runInNewContext(source, { window, document, CustomEvent: class {} });
  const container = {
    dataset: {}, innerHTML: '',
    querySelector(key) {
      if (!nodes.has(key)) nodes.set(key, { addEventListener(_type, callback) { this.click = callback; } });
      return nodes.get(key);
    },
    querySelectorAll() { return [{ dataset: { pluginId: 'review' } }]; },
  };
  const api = async (url, options) => {
    calls.push([url, options]);
    if (options?.method === 'PUT') return { plugin_ids: ['review'], policy_warnings: [], settings: {} };
    return { can_import: false, enabled_plugins: [], plugins: [{
      id: 'review', name: '<script>name</script>', description: '" onmouseover="bad()',
      capabilities: { skills: [], tools: ['web_search'], mcp_servers: [], models: [] },
    }] };
  };
  return { window, container, api, calls, nodes };
}

test('mount is read-only, collapsed, safely escaped and hides admin imports', async () => {
  const f = fixture();
  await f.window.OdysseusPluginCatalog.mount(f.container, { sessionId: 'chat', api: f.api });
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0][1], undefined);
  assert.ok(f.container.innerHTML.includes('&quot; onmouseover=&quot;bad()'));
  assert.ok(f.container.innerHTML.includes('&lt;script&gt;name&lt;/script&gt;'));
  assert.ok(!f.container.innerHTML.includes('title="" onmouseover='));
  assert.ok(!f.container.innerHTML.includes('data-plugin-import-button'));
  assert.ok(!/<details[^>]*\bopen\b/.test(f.container.innerHTML));
});

test('apply requires confirmation and refreshes only after success', async () => {
  const f = fixture();
  let refreshed = 0;
  await f.window.OdysseusPluginCatalog.mount(f.container, {
    sessionId: 'chat', api: f.api, onApplied: () => { refreshed++; },
  });
  await f.nodes.get('[data-plugin-preview]').click();
  assert.equal(f.calls.length, 1);
  f.window.confirm = () => true;
  await f.nodes.get('[data-plugin-preview]').click();
  assert.equal(f.calls.length, 2);
  assert.equal(f.calls[1][1].method, 'PUT');
  assert.equal(refreshed, 1);
});

function v2Fixture(canImport, installs = {}) {
  const calls = [];
  const window = { confirm: () => true, dispatchEvent() {} };
  const document = { createElement() { return {
    textContent: '', get innerHTML() {
      return this.textContent.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    },
  }; } };
  vm.runInNewContext(source, { window, document, CustomEvent: class {} });
  const handlers = {};
  const stub = () => ({ addEventListener(type, cb) { handlers[type] = cb; }, dataset: {}, querySelector: () => null,
    querySelectorAll: () => [] });
  const form = { dataset: { pluginInstallForm: 'demo' }, addEventListener(_t, cb) { handlers.submit = cb; },
    querySelector: () => ({ checked: true }),
    querySelectorAll: () => [{ dataset: { envName: 'DEMO_TOKEN' }, value: 'sk-1' }] };
  const container = { dataset: {}, innerHTML: '',
    querySelector: () => stub(),
    querySelectorAll(key) { return key === '[data-plugin-install-form]' ? [form] : []; } };
  const api = async (url, options) => {
    calls.push([url, options]);
    if (options?.method === 'POST') return { server: { connected: true }, skills: [{}], loadouts: [], unresolved_references: [], loadout_errors: [] };
    return { can_import: canImport, enabled_plugins: [], installs, plugins: [{
      id: 'demo', name: 'Demo', description: '',
      capabilities: { skills: [], tools: [], mcp_servers: [], models: [] },
      integration: { name: 'Demo', instructions: '<b>x</b>', skills: [{ name: 'demo-howto' }], loadout_templates: [],
        mcp_server: { name: 'Demo Server', transport: 'stdio', command: 'npx', args: ['-y', 'demo-mcp'],
          env: [{ name: 'DEMO_TOKEN', description: 'API token', required: true }] } },
    }] };
  };
  return { window, container, api, calls, handlers };
}

test('v2 plugin shows its command, env names, skills and an admin-only install form', async () => {
  const admin = v2Fixture(true);
  await admin.window.OdysseusPluginCatalog.mount(admin.container, { sessionId: 'chat', api: admin.api });
  const html = admin.container.innerHTML;
  assert.ok(html.includes('npx -y demo-mcp'));
  assert.ok(html.includes('DEMO_TOKEN') && html.includes('demo-howto'));
  assert.ok(html.includes('data-plugin-install-form="demo"'));
  assert.ok(html.includes('type="password"'));
  assert.ok(html.includes('&lt;b&gt;x&lt;/b&gt;'));
  const viewer = v2Fixture(false);
  await viewer.window.OdysseusPluginCatalog.mount(viewer.container, { sessionId: 'chat', api: viewer.api });
  assert.ok(viewer.container.innerHTML.includes('npx -y demo-mcp'));
  assert.ok(!viewer.container.innerHTML.includes('data-plugin-install-form'));
});

test('install posts env values with the approved command and needs confirmation', async () => {
  const f = v2Fixture(true);
  await f.window.OdysseusPluginCatalog.mount(f.container, { sessionId: 'chat', api: f.api });
  await f.handlers.submit({ preventDefault() {} });
  const post = f.calls.find(([, o]) => o?.method === 'POST');
  assert.equal(post[0], '/api/plugins/demo/install');
  const body = JSON.parse(post[1].body);
  assert.deepEqual(body.env, { DEMO_TOKEN: 'sk-1' });
  assert.equal(body.approved, 'npx -y demo-mcp');
  assert.equal(body.publish_skills, true);
});

test('installed plugin offers uninstall instead of the form', async () => {
  const f = v2Fixture(true, { demo: { server_id: 'srv1', skills: ['demo-howto'], loadouts: [] } });
  await f.window.OdysseusPluginCatalog.mount(f.container, { sessionId: 'chat', api: f.api });
  assert.ok(f.container.innerHTML.includes('data-plugin-uninstall="demo"'));
  assert.ok(!f.container.innerHTML.includes('data-plugin-install-form'));
});
