// The Context tab in Settings loads its presets from, and saves a choice to,
// /api/auth/settings/context-profile (routes/auth_routes.py, tested in
// tests/routes/auth_routes/test_context_profile.py). The tab once called
// /api/settings/context-profile, which nothing serves: every load 404'd and
// the tab could only say it could not load.
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

const PROFILE = {
  presets: { lean: { label: 'Lean', hint: 'Small windows' }, roomy: { label: 'Roomy', hint: 'Large windows' } },
  recommended: 'roomy',
  selected: '',
  knobs: { tool_result_inline_chars: { label: 'Inline tool output', help: 'Characters kept inline', default: 4000, min: 0, max: 100000 } },
  effective: { tool_result_inline_chars: 4000, _source: 'auto' },
  custom_values: {},
  context_length: 128000,
};
const saves = [];
fake.route('GET', '/api/auth/settings', () => ({}));
fake.route('GET', '/api/auth/settings/context-profile', () => PROFILE);
fake.route('POST', '/api/auth/settings/context-profile', ({ body }) => {
  saves.push(JSON.parse(body));
  return { saved: '*', context_profiles: {} };
});

const settings = await import('../../../../static/js/settings.js');
settings.open('context');

const presets = () => document.getElementById('set-ctxPresets');
const message = () => document.getElementById('set-ctxMsg');

test('the tab loads its presets from the context-profile route', async () => {
  await waitFor(() => presets().querySelector('#ctx-preset-lean'), { what: 'the presets' });
  assert.equal(message().textContent, '');
});

test('choosing a preset and saving posts it to the same route', async () => {
  const lean = presets().querySelector('#ctx-preset-lean');
  lean.checked = true;
  lean.dispatchEvent(new Event('change'));
  document.getElementById('set-ctxSave').click();

  await waitFor(() => saves.length > 0, { what: 'the save' });
  assert.equal(saves[0].preset, 'lean');
  await waitFor(() => message().textContent !== '', { what: 'the save message' });
  assert.notEqual(message().style.color, 'var(--danger, #d66)', message().textContent);
});
