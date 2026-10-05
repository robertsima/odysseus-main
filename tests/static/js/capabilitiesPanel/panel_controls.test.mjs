// Settings > Advanced, rendered by static/js/capabilitiesPanel.js from the
// declared settings schema. The capability list starts closed (even when a
// capability needs setup) and stays as the user left it across re-renders;
// "Show advanced" brings advanced settings into view; settings whose values
// come from what is installed are picked from a list, not typed.
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

document.body.innerHTML = '<div id="capabilities-panel-body"></div>';
const toasts = [];
window.uiModule = { showToast: (msg) => toasts.push(msg) };

fake.route('GET', '/api/capabilities', () => ({
  capabilities: [
    { name: 'vision', title: 'Vision', summary: 'Read images', enabled: true, satisfied: false, available: false,
      unmet: [{ requirement: 'model', detail: 'No vision model', hint: 'Add one' }] },
  ],
}));
fake.route('GET', '/api/settings/schema', () => ({
  is_admin: true,
  groups: [
    { group: 'General', settings: [
      { key: 'default_endpoint_id', label: 'Default endpoint', type: 'text', options_source: 'endpoints', value: '', default: '' },
      { key: 'default_model', label: 'Default model', type: 'text', options_source: 'models', value: '', default: '' },
      { key: 'tts_voice', label: 'Voice', type: 'text', suggestions: ['alloy', 'echo'], value: '', default: '' },
    ] },
    { group: 'Expert', settings: [
      { key: 'plain_knob', label: 'Plain knob', type: 'text', value: '', default: '' },
      { key: 'deep_knob', label: 'Deep knob', type: 'int', advanced: true, value: 3, default: 3 },
    ] },
  ],
}));
fake.route('GET', '/api/model-endpoints', () => [
  { id: 7, name: 'Local box', models: ['qwen-7b', 'llama-8b'] },
]);

const panel = await import('../../../../static/js/capabilitiesPanel.js');
panel.mount('capabilities');

const host = document.getElementById('capabilities-panel-body');
const section = () => host.querySelector('details.cap-section');
const row = (key) => host.querySelector(`[data-setting-row="${key}"]`);
await waitFor(() => section() && row('default_endpoint_id'), { what: 'the panel to render' });

test('the capability list starts closed, even with a capability that needs setup', () => {
  assert.equal(section().open, false);
});

test('the capability list stays open after a re-render once the user opened it', () => {
  section().open = true;
  section().dispatchEvent(new Event('toggle'));
  // Changing category re-renders the whole panel.
  const filter = document.getElementById('settings-group-filter');
  filter.value = 'General';
  filter.dispatchEvent(new Event('change', { bubbles: true }));

  assert.notEqual(document.getElementById('settings-group-filter'), filter, 'the panel re-rendered');
  assert.equal(section().open, true);
});

test('installed endpoints and models are offered in a list; a free value gets suggestions', () => {
  const endpoint = row('default_endpoint_id').querySelector('[data-set-key]');
  assert.equal(endpoint.tagName, 'SELECT');
  assert.ok([...endpoint.options].some((o) => o.value === '7'), 'the endpoint is listed by id');

  const model = row('default_model').querySelector('[data-set-key]');
  assert.equal(model.tagName, 'SELECT');
  assert.deepEqual([...model.options].map((o) => o.value).filter(Boolean), ['llama-8b', 'qwen-7b']);

  const voice = row('tts_voice').querySelector('input[data-set-key]');
  const list = document.getElementById(voice.getAttribute('list'));
  assert.deepEqual([...list.options].map((o) => o.value), ['alloy', 'echo']);
});

test('Show advanced moves to a category that has advanced settings and shows them', () => {
  assert.equal(row('deep_knob'), null);
  toasts.length = 0;

  host.querySelector('[data-cap-action="advanced"]').click();

  assert.ok(row('deep_knob'), 'the advanced setting is visible');
  assert.equal(document.getElementById('settings-group-filter').value, 'Expert');
  assert.equal(toasts.length, 1, 'the change is confirmed');
});
