// Settings > Privacy decides which model scopes may read Lotus wellbeing data
// (local, LAN, public API). The toggles show the stored policy and Save writes
// it to PUT /api/lotus/access-policy. Allowing public API models sends that
// data to a third-party provider, so turning it on needs an explicit
// confirmation, and declining must leave the stored policy unchanged.
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

fake.route('GET', '/api/lotus/access-policy', () => ({ local: true, lan: false, api: false }));
fake.route('PUT', '/api/lotus/access-policy', ({ body }) => JSON.parse(body));

let confirmAnswer = false;
const confirmations = [];
window.confirm = (text) => { confirmations.push(text); return confirmAnswer; };

mountSettingsModal();
const { default: settingsModule } = await import('../../../../static/js/settings.js');
const byId = (id) => document.getElementById(id);
const puts = () => fake.calls.filter((c) => c.method === 'PUT' && c.url.pathname === '/api/lotus/access-policy');

test('the Privacy tab shows the stored Lotus access policy', async () => {
  settingsModule.open('privacy');
  await waitFor(() => byId('set-lotus-access-local').checked, { what: 'the stored policy' });

  assert.equal(byId('set-lotus-access-lan').checked, false);
  assert.equal(byId('set-lotus-access-api').checked, false);
});

test('saving writes the toggles to the access-policy endpoint', async () => {
  byId('set-lotus-access-lan').checked = true;
  byId('set-lotus-access-save').click();
  await waitFor(() => puts().length === 1, { what: 'the save' });

  assert.deepEqual(JSON.parse(puts()[0].body), { local: true, lan: true, api: false });
  assert.deepEqual(confirmations, [], 'local and LAN access need no confirmation');
});

test('allowing public API models asks first, and a no saves nothing', async () => {
  await waitFor(() => !byId('set-lotus-access-save').disabled, { what: 'the previous save to finish' });
  byId('set-lotus-access-api').checked = true;
  confirmAnswer = false;
  byId('set-lotus-access-save').click();
  await new Promise((resolve) => setTimeout(resolve, 20));

  assert.equal(confirmations.length, 1);
  assert.equal(puts().length, 1, 'declining sends no request');

  confirmAnswer = true;
  byId('set-lotus-access-save').click();
  await waitFor(() => puts().length === 2, { what: 'the confirmed save' });
  assert.deepEqual(JSON.parse(puts()[1].body), { local: true, lan: true, api: true });
});
