// A theme this browser already has paints before the account sync answers.
// Waiting on /api/prefs first would hold the page on the half-styled
// first-paint colors for a network round trip.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { waitFor } from '../_support/dom.mjs';
import { bootTheme, THEME_KEY } from './_account.mjs';

const page = bootTheme({
  hold: true,
  local: {
    [THEME_KEY]: {
      name: 'dark',
      colors: { bg: '#123456', fg: '#fedcba', panel: '#1a1a1a', border: '#2a2a2a', red: '#ff5555' },
      updated_at: 100,
    },
  },
});
after(async () => {
  page.release();
  await page.restore();
});

await import('../../../../static/js/theme.js');

test('the local theme is applied while the account request is still open', async () => {
  await waitFor(() => page.fake.calls.some((c) => c.url.pathname.startsWith('/api/prefs/')),
    { what: 'the account sync to start' });
  assert.equal(page.cssVar('--bg'), '#123456');
  assert.equal(page.cssVar('--fg'), '#fedcba');
});
