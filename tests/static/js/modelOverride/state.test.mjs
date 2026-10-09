import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { installDom, waitFor } from '../_support/dom.mjs';
const dom = installDom();
const { enhanceModelControl, catalogChoices } = await import('../../../../static/js/modelOverride.js');
after(() => dom.restore());
const CUSTOM = '__odysseus_custom_model__';
function choose(el, value) { el.value = value; el.dispatchEvent(new Event('change', { bubbles: true })); }
test('custom select value survives discovery replacement and default never submits a sentinel', async () => {
  document.body.innerHTML = '<select id="override"><option value="saved">saved</option></select>';
  const select = document.querySelector('select');
  enhanceModelControl(select);
  choose(select, CUSTOM);
  const input = select.parentElement.querySelector('input');
  input.value = 'private-model'; input.dispatchEvent(new Event('input', { bubbles: true }));
  assert.equal(select.value, 'private-model');
  // Endpoint discovery preserves the current value before replacing options.
  select.innerHTML = '<option value="private-model" selected>private-model</option><option value="new">new</option>';
  await waitFor(() => [...select.options].some(o => o.value === CUSTOM));
  assert.equal(input.value, 'private-model');
  choose(select, ''); assert.equal(select.value, ''); assert.equal(input.hidden, true);
  choose(select, CUSTOM); assert.equal(input.value, '');
});
test('editing an input before catalog arrival is not overwritten and catalog choice dispatches submission listeners', async () => {
  let resolve;
  globalThis.fetch = () => new Promise(r => { resolve = r; });
  document.body.innerHTML = '<input id="manual" value="saved-custom">';
  const input = document.querySelector('input');
  let changes = 0; input.addEventListener('change', () => changes++);
  enhanceModelControl(input, { loadCatalog: true });
  input.value = 'edited-custom'; input.dispatchEvent(new Event('input', { bubbles: true }));
  resolve(new Response(JSON.stringify({items:[{endpoint_name:'local', models:['qwen']}] }), {status:200}));
  const select = input.parentElement.querySelector('select');
  await waitFor(() => [...select.options].some(o => o.value === 'qwen@local'));
  assert.equal(input.value, 'edited-custom'); assert.equal(select.value, CUSTOM);
  choose(select, 'qwen@local'); assert.equal(input.value, 'qwen@local'); assert.equal(changes, 1);
  choose(select, ''); assert.equal(input.value, '');
});
test('catalog includes extra models, excludes offline/non-chat, and keeps endpoint addressing', () => {
  assert.deepEqual(catalogChoices({items:[{endpoint_name:'a',models:['x'],models_extra:['y']},{offline:true,models:['bad']},{model_type:'image',models:['bad']}] }).map(c=>c.value), ['x@a','y@a']);
});
test('ordered model lists append once and default clears the submitted list', async () => {
  document.body.innerHTML = '<textarea>first@local</textarea>';
  const control = document.querySelector('textarea');
  enhanceModelControl(control, { loadCatalog: true });
  const select = control.parentElement.querySelector('select');
  await waitFor(() => [...select.options].some(o => o.value === 'qwen@local'));
  choose(select, 'qwen@local'); choose(select, 'qwen@local');
  assert.equal(control.value, 'first@local\nqwen@local');
  control.disabled = true;
  await waitFor(() => select.disabled);
  control.disabled = false;
  await waitFor(() => !select.disabled);
  choose(select, ''); assert.equal(control.value, '');
});
