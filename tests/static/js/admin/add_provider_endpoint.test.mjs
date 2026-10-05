// Settings > Models > Add provider, driven through the real form from
// static/index.html. Device-auth providers (GitHub Copilot, ChatGPT
// Subscription) sign in when Add is clicked, not when they are picked, and
// show an authorize link instead of opening a tab. Google Gemini is added
// without a model refresh mode so the backend's manual default applies.
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

const opened = [];
window.open = (...args) => { opened.push(args); return null; };

const DEVICE_START = {
  poll_id: 'poll-1',
  user_code: 'WXYZ-1234',
  verification_uri: 'https://github.com/login/device',
  interval: 5,
  expires_in: 900,
};
fake.route('GET', '/api/model-endpoints', () => []);
fake.route('POST', '/api/copilot/device/start', () => DEVICE_START);
fake.route('POST', '/api/model-endpoints', () => ({ id: 'ep-1', online: true, models: [] }));

const admin = await import('../../../../static/js/admin.js');
admin._initData('models');

const el = (id) => document.getElementById(id);
const callsTo = (path) => fake.calls.filter((c) => c.url.pathname === path);

function pickProvider(value) {
  const provider = el('adm-epProvider');
  assert.ok([...provider.options].some((o) => o.value === value), `index.html offers ${value}`);
  provider.value = value;
  provider.dispatchEvent(new Event('change', { bubbles: true }));
}

async function addEndpoint(baseUrl) {
  pickProvider(baseUrl);
  el('adm-epApiKey').value = 'sk-test';
  const before = callsTo('/api/model-endpoints').filter((c) => c.method === 'POST').length;
  el('adm-epAddBtn').click();
  await waitFor(() => callsTo('/api/model-endpoints').filter((c) => c.method === 'POST').length > before,
    { what: 'the endpoint POST' });
  return callsTo('/api/model-endpoints').filter((c) => c.method === 'POST').at(-1).body;
}

test('Google Gemini is added without a refresh mode, other providers with auto', async () => {
  const google = await addEndpoint('https://generativelanguage.googleapis.com/v1beta/openai');
  assert.equal(google.has('model_refresh_mode'), false);

  const openai = await addEndpoint('https://api.openai.com/v1');
  assert.equal(openai.get('model_refresh_mode'), 'auto');
});

test('a device-auth provider disables the API test button until another provider is picked', () => {
  pickProvider('copilot');
  assert.equal(el('adm-epApiTestBtn').disabled, true);
  pickProvider('https://api.openai.com/v1');
  assert.equal(el('adm-epApiTestBtn').disabled, false);
});

test('picking GitHub Copilot starts nothing; Add starts the sign-in and shows the link', async () => {
  pickProvider('copilot');
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.deepEqual(callsTo('/api/copilot/device/start'), [], 'picking the provider is inert');

  el('adm-epAddBtn').click();
  await waitFor(() => el('adm-deviceAuthStatus').querySelector('a[href]'), { what: 'the authorize link' });

  assert.equal(callsTo('/api/copilot/device/start').length, 1);
  const link = el('adm-deviceAuthStatus').querySelector('a[href]');
  assert.equal(link.getAttribute('href'), DEVICE_START.verification_uri);
  assert.equal(link.getAttribute('target'), '_blank');
  assert.match(el('adm-deviceAuthStatus').textContent, /WXYZ-1234/);
  assert.deepEqual(opened, [], 'no tab opens on its own');
});
