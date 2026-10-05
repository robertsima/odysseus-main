// Saving a character in static/js/presets.js updates the in-memory template
// list at once, before POST /api/presets/templates settles, so the Group
// picker can offer it straight away (#3207). When that POST fails, an edited
// character must get its old fields back rather than keep the unsaved ones.
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

const SAVED = [{ id: 'user-1', name: 'Nova', system_prompt: 'old prompt', temperature: 0.5, max_tokens: 100 }];
let templatePost = () => new Promise(() => {});

fake.route('GET', '/api/presets/templates', () => SAVED.map((t) => ({ ...t })));
fake.route('POST', '/api/presets/custom', () => ({ success: true }));
fake.route('POST', '/api/presets/templates', () => templatePost());

document.body.innerHTML = `
  <input id="custom-character-name">
  <input id="custom-temperature" value="1.0">
  <input id="custom-max-tokens" value="0">
  <textarea id="custom-system-prompt"></textarea>
`;

const presets = await import('../../../../static/js/presets.js');
presets.init('');
await waitFor(() => presets.getUserTemplates().length === 1, { what: 'the saved templates to load' });

function fillForm(name, prompt) {
  document.getElementById('custom-character-name').value = name;
  document.getElementById('custom-system-prompt').value = prompt;
}

test('a new character is in the template list before its save settles', async () => {
  fillForm('Atlas', 'You are Atlas.');
  await presets.saveCustomPreset();

  const atlas = presets.getUserTemplates().find((t) => t.name === 'Atlas');
  assert.ok(atlas, 'the new character is listed while POST /api/presets/templates is pending');
  assert.equal(atlas.system_prompt, 'You are Atlas.');
  assert.ok(atlas.id, 'the optimistic entry has an id the Group picker can select by');
});

test('a failed save gives an edited character its old fields back', async () => {
  let failed;
  const settled = new Promise((resolve) => { failed = resolve; });
  templatePost = () => { failed(); throw new TypeError('network down'); };
  const errors = [];

  fillForm('Nova', 'new prompt');
  await presets.saveCustomPreset(() => {}, (msg) => errors.push(msg));
  await settled;
  await waitFor(() => errors.length > 0, { what: 'the save error to be reported' });

  const nova = presets.getUserTemplates().find((t) => t.name === 'Nova');
  assert.equal(nova.system_prompt, 'old prompt');
  assert.equal(nova.temperature, 0.5);
  assert.equal(nova.max_tokens, 100);
});
