// Saving a theme stamps it with its write time and sends it to the account,
// and the text size is saved as part of it. The stamp is what lets two
// browsers decide which copy is newer; without the text size in the theme, a
// second browser gets the colors and keeps its own text size.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { waitFor } from '../_support/dom.mjs';
import { bootTheme, settled, THEME_KEY } from './_account.mjs';

const COLORS = { bg: '#0b0b0b', fg: '#c0ffee', panel: '#151515', border: '#252525', red: '#ff5555' };

const page = bootTheme({
  local: { [THEME_KEY]: { name: 'dark', colors: COLORS, updated_at: 100 } },
  account: { theme: { value: { name: 'dark', colors: COLORS }, updated_at: 100 } },
});
after(() => page.restore());

const theme = await import('../../../../static/js/theme.js');
await waitFor(() => page.fake.calls.some((c) => c.url.pathname === '/api/prefs/page-style'),
  { what: 'the boot sync to finish' });
await settled();

test('a saved theme carries its write time, locally and in the account', async () => {
  page.puts.length = 0;
  const before = Date.now();
  theme.save('cute', COLORS);
  const stored = page.savedTheme();
  assert.ok(stored.updated_at >= before, 'the local copy is stamped');

  await waitFor(() => page.puts.length > 0, { what: 'the theme PUT' });
  const [put] = page.puts;
  assert.equal(put.key, 'theme');
  assert.equal(put.updated_at, stored.updated_at);
  assert.equal(put.value.name, 'cute');
});

test('changing the text size saves it into the account theme', async () => {
  page.puts.length = 0;
  const select = document.getElementById('theme-text-size-select');
  select.value = '125';
  select.dispatchEvent(new Event('change'));

  await waitFor(() => page.puts.length > 0, { what: 'the theme PUT' });
  assert.equal(page.puts.at(-1).value.uiScale, '125');
});
