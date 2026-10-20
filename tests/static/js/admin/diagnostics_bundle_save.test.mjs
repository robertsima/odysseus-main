// The diagnostics export (static/js/admin.js initDiagnosticsBundle) starts its
// download after the bundle is built, outside the click that asked for it.
// 2026-10-20: the server built bundles but they never reached the person's
// Downloads, and the page said "Downloaded". A finished export must leave a
// Save link the person can click themselves.
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

fake.route('GET', /^\/api\/diagnostics\/bundle$/, () => new Response(new Uint8Array([80, 75, 3, 4]), {
  status: 200,
  headers: { 'Content-Type': 'application/zip', 'Content-Disposition': 'attachment; filename="odysseus-diagnostics-test.zip"' },
}));

// The fake DOM's Blob is not the one Node's URL.createObjectURL accepts.
const objectUrls = [];
URL.createObjectURL = (blob) => { objectUrls.push(blob); return `blob:test/${objectUrls.length}`; };
URL.revokeObjectURL = () => {};

const admin = await import('../../../../static/js/admin.js');

test('a finished export offers a Save link for the bundle', async () => {
  admin._initData('system');
  document.getElementById('diag-bundle-export-btn').click();

  const msg = document.getElementById('diag-bundle-msg');
  await waitFor(() => msg.querySelector('a.settings-diag-bundle-save'), { what: 'the Save link' });
  const save = msg.querySelector('a.settings-diag-bundle-save');
  assert.equal(save.getAttribute('download'), 'odysseus-diagnostics-test.zip');
  assert.equal(save.getAttribute('href'), `blob:test/${objectUrls.length}`);
  assert.doesNotMatch(msg.textContent, /^Downloaded/);
});
