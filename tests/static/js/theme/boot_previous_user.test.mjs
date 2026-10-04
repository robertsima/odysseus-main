// A browser shared by two accounts: "al" used it last, "bea" is signed in
// now, and al's theme and custom themes are still in localStorage. Boot must
// not push al's copies up as bea's, even when they look newer than bea's.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { waitFor } from '../_support/dom.mjs';
import { bootTheme, settled, THEME_KEY } from './_account.mjs';

const ALS_COLORS = { bg: '#202020', fg: '#f0f0f0', panel: '#2a2a2a', border: '#3a3a3a', red: '#ff5555' };

const page = bootTheme({
  liveUser: 'bea',
  local: {
    'odysseus-auth-user': 'al',
    [THEME_KEY]: { name: 'dark', colors: ALS_COLORS, updated_at: 999 },
    'odysseus-custom-themes': { 'al-night': ALS_COLORS },
    'odysseus-custom-themes-updated': '999',
  },
  // bea has never saved a theme, and has one older custom theme.
  account: { 'custom-themes': { value: { 'bea-day': ALS_COLORS }, updated_at: 1 } },
});
after(() => page.restore());

await import('../../../../static/js/theme.js');

test("the previous user's theme does not seed an empty account", async () => {
  await waitFor(() => page.fake.calls.some((c) => c.url.pathname === '/api/prefs/page-style'),
    { what: 'the boot sync to finish' });
  await settled();
  assert.deepEqual(page.puts.filter((p) => p.key === 'theme'), []);
});

test("the previous user's custom themes do not replace the account's", async () => {
  await waitFor(() => page.fake.calls.some((c) => c.url.pathname === '/api/prefs/page-style'),
    { what: 'the boot sync to finish' });
  await settled();
  assert.deepEqual(page.puts.filter((p) => p.key === 'custom-themes'), []);
  assert.deepEqual(Object.keys(JSON.parse(localStorage.getItem('odysseus-custom-themes'))), ['bea-day']);
});
