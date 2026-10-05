// Picking the default chat model in Settings saves the endpoint and model and
// nothing else. The retired default-fallback editor used to ride along on this
// save, so changing the model rewrote default_model_fallbacks from an editor
// that no longer showed them, and the stored fallbacks were lost.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';
import { mountSettingsModal } from './_modal.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

fake.route('GET', '/api/model-endpoints', () => [
  { id: 'ep1', name: 'Local', is_enabled: true, online: true, models: ['model-a', 'model-b'] },
]);
fake.route('GET', '/api/auth/settings', () => ({
  default_endpoint_id: 'ep1',
  default_model: 'model-a',
  default_model_fallbacks: [{ endpoint_id: 'ep1', model: 'model-b' }],
}));
fake.route('POST', '/api/auth/settings', () => ({ ok: true }));

mountSettingsModal();
const { default: settingsModule } = await import('../../../../static/js/settings.js');

test('changing the default model posts only the endpoint and the model', async () => {
  settingsModule.open('models');
  const modelSelect = document.getElementById('set-defaultModelSelect');
  await waitFor(() => modelSelect.value === 'model-a', { what: 'the saved default model' });

  modelSelect.value = 'model-b';
  modelSelect.dispatchEvent(new Event('change'));

  const saves = () => fake.calls.filter((c) => c.method === 'POST' && c.url.pathname === '/api/auth/settings');
  await waitFor(() => saves().length, { what: 'the save' });
  assert.deepEqual(JSON.parse(saves()[0].body), { default_endpoint_id: 'ep1', default_model: 'model-b' });
});
