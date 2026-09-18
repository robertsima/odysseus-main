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
