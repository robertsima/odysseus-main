// The Group tab's participant picker in static/js/group.js lists characters
// from GET /api/presets/templates plus the ones presets.js holds in memory.
// A character saved a moment ago is not on the server yet while its template
// POST is in flight; without the in-memory merge it was missing from the
// dropdown (#3207).
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

let serverTemplates = [];
fake.route('GET', '/api/presets/templates', () => serverTemplates);
fake.route('GET', '/api/models', () => ({ items: [{ url: 'http://local/v1', models: ['m1'] }] }));
fake.route('POST', '/api/presets/custom', () => ({ success: true }));
fake.route('POST', '/api/presets/templates', () => new Promise(() => {}));

document.body.innerHTML = `
  <input id="custom-character-name">
  <input id="custom-temperature" value="1.0">
  <input id="custom-max-tokens" value="0">
  <textarea id="custom-system-prompt"></textarea>
  <div id="group-participants"></div>
  <button id="group-add-btn"></button>
`;

const presets = await import('../../../../static/js/presets.js');
const group = await import('../../../../static/js/group.js');
group.init('');
// group.js wires the Group tab 500 ms after init.
await new Promise((resolve) => setTimeout(resolve, 600));

test('a character saved moments ago is offered while its template save is pending', async () => {
  document.getElementById('custom-character-name').value = 'Atlas';
  document.getElementById('custom-system-prompt').value = 'You are Atlas.';
  await presets.saveCustomPreset();

  document.getElementById('group-add-btn').click();
  let select;
  await waitFor(() => (select = document.querySelector('select[data-selection-type="character"]')),
    { what: 'the character dropdown' });

  const names = [...select.options].map((o) => o.textContent);
  assert.ok(names.includes('Atlas'), `Atlas missing from ${JSON.stringify(names)}`);
  select.closest('div').remove();
});

test('a character the server already returns is listed once', async () => {
  const atlas = presets.getUserTemplates().find((t) => t.name === 'Atlas');
  serverTemplates = [{ ...atlas }];

  document.getElementById('group-add-btn').click();
  let select;
  await waitFor(() => (select = document.querySelector('select[data-selection-type="character"]')),
    { what: 'the character dropdown' });

  const names = [...select.options].map((o) => o.textContent);
  assert.deepEqual(names.filter((n) => n === 'Atlas'), ['Atlas']);
});
