// The chat model picker in static/js/modelPicker.js.
// - Long model names are clipped with an ellipsis, so the dropdown row and the
//   header label carry the full name as a hover title (#1982).
// - The same model id enabled on two API endpoints (OpenAI and OpenRouter)
//   is two choices; deduping by model id alone dropped one of them.
// - In a large catalog, an API endpoint's models are grouped under the
//   endpoint's name, not split by the vendor prefix of each model id.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

let items = [];
let session = { id: 's1', model: '' };
window.modelsModule = { getCachedItems: () => items };

document.body.innerHTML = `
  <div id="model-picker-wrap">
    <button id="model-picker-btn"><span id="model-picker-label"></span></button>
    <div id="model-picker-menu" class="hidden">
      <div class="model-picker-search-row"><input id="model-picker-search"></div>
      <div id="model-picker-list"></div>
    </div>
  </div>
`;

const { initModelPicker, updateModelPicker } = await import('../../../../static/js/modelPicker.js');
initModelPicker({
  getCurrentSessionId: () => session.id,
  getSessions: () => [session],
  getPendingChat: () => null,
  setPendingChat: () => {},
  createDirectChat: () => {},
});

const list = document.getElementById('model-picker-list');

function showPicker() {
  const menu = document.getElementById('model-picker-menu');
  if (menu.classList.contains('hidden')) {
    document.getElementById('model-picker-btn').click();
  } else {
    const search = document.getElementById('model-picker-search');
    search.value = '';
    search.dispatchEvent(new Event('input'));
  }
}

const LONG_ID = 'acme/very-long-model-name-with-a-variant-suffix-instruct-q4_k_m';

test('a dropdown row carries the full model name as its title', () => {
  items = [{ url: 'http://local:8080/v1', endpoint_id: 'ep-local', category: 'local', models: [LONG_ID] }];
  showPicker();

  const name = list.querySelector('.model-switch-item .mp-model-name');
  assert.equal(name.textContent, 'very-long-model-name-with-a-variant-suffix-instruct-q4_k_m');
  assert.equal(name.title, name.textContent);
});

test('the header label carries the full model id as its title, and none for the placeholder', () => {
  const label = document.getElementById('model-picker-label');

  session = { id: 's1', model: LONG_ID };
  updateModelPicker();
  assert.equal(label.title, LONG_ID);

  session = { id: 's1', model: '' };
  updateModelPicker();
  assert.equal(label.textContent, 'Select model');
  assert.equal(label.title, '');
});

test('the same model id on two API endpoints is offered once per endpoint', () => {
  items = [
    { url: 'https://api.openai.com/v1', endpoint_id: 'ep-openai', endpoint_name: 'OpenAI', category: 'api', models: ['gpt-4o'] },
    { url: 'https://openrouter.ai/api/v1', endpoint_id: 'ep-or', endpoint_name: 'OpenRouter', category: 'api', models: ['gpt-4o'] },
  ];
  showPicker();

  const rows = [...list.querySelectorAll('.model-switch-item')].map((row) => [
    row.querySelector('.mp-model-name').textContent,
    row.querySelector('.model-switch-ep').textContent,
  ]);
  assert.deepEqual(rows.sort(), [['gpt-4o', 'OpenAI'], ['gpt-4o', 'OpenRouter']]);
});

test("a large catalog groups an API endpoint's models under the endpoint name", () => {
  const ids = Array.from({ length: 14 }, (_, i) => (i % 2 ? `openai/gpt-${i}` : `google/gemini-${i}`));
  items = [{ url: 'https://openrouter.ai/api/v1', endpoint_id: 'ep-or', endpoint_name: 'OpenRouter', category: 'api', models: ids }];
  showPicker();

  const groups = [...list.querySelectorAll('.mp-provider-header .mp-provider-name')].map((el) => el.textContent);
  assert.deepEqual(groups, ['OpenRouter']);
});
