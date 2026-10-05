// Settings > Account > Set Up 2FA renders the secret and the QR code the
// server returned into the page. Both are written into HTML, so the secret
// must be shown as text and the QR image may only be a raster data URL: an SVG
// or a value that breaks out of the src attribute would run script inside the
// account settings.
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

const PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';
const setups = [
  { secret: '<img src=x onerror="window.__pwned=1">JBSWY3DP', qr_code: 'data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9YWxlcnQoMSk+PC9zdmc+' },
  { secret: 'JBSWY3DP', qr_code: `${PNG}" onerror="window.__pwned=1` },
  { secret: 'JBSWY3DP', qr_code: PNG },
];
fake.route('GET', '/api/auth/2fa/status', () => ({ enabled: false }));
fake.route('POST', '/api/auth/2fa/setup', () => setups.shift());

mountSettingsModal();
const { default: settingsModule } = await import('../../../../static/js/settings.js');
const content = () => document.getElementById('settings-2fa-content');

async function startSetup() {
  await waitFor(() => document.getElementById('tfa-setup-btn'), { what: 'the Set Up 2FA button' });
  document.getElementById('tfa-setup-btn').click();
  await waitFor(() => document.getElementById('tfa-verify-code'), { what: 'the setup step' });
}

async function cancelSetup() {
  document.getElementById('tfa-cancel-btn').click();
}

test('markup in the secret is shown as text and an SVG QR code is not rendered', async () => {
  settingsModule.open('account');
  await startSetup();

  assert.equal(content().querySelectorAll('img').length, 0);
  assert.ok(content().textContent.includes('<img src=x onerror="window.__pwned=1">JBSWY3DP'));
  await cancelSetup();
});

test('a QR value that breaks out of the src attribute is not rendered', async () => {
  await startSetup();
  assert.equal(content().querySelectorAll('img').length, 0);
  assert.equal(content().querySelector('[onerror]'), null);
  await cancelSetup();
});

test('a PNG QR code is rendered', async () => {
  await startSetup();
  const images = [...content().querySelectorAll('img')];
  assert.deepEqual(images.map((img) => img.getAttribute('src')), [PNG]);
});
