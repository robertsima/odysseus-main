// A blank slate takes the page style's typeface; an explicit font is kept.
//
// The default font used to be Monospace, written inline as --font-family on
// every boot, while the Agamemnon stylesheet pinned Space Grotesk on the body.
// A new user saw two typefaces, and picking a font changed only the few rules
// that read --font-family (2026-10-07). "Theme default" now leaves
// --font-family unset, so style.css resolves one --ui-font for the page.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { waitFor } from '../_support/dom.mjs';
import { bootTheme, settled } from './_account.mjs';

const page = bootTheme();
page.fake.route('GET', '/api/fonts/custom', () => ({ fonts: {} }));
after(() => page.restore());
await import('../../../../static/js/theme.js');
await waitFor(() => page.fake.calls.some((c) => c.url.pathname === '/api/prefs/page-style'),
  { what: 'the boot sync to finish' });
await settled();

test('a blank slate selects Theme default and sets no font of its own', async () => {
  await waitFor(() => document.getElementById('theme-font-select')?.value === 'auto', { what: 'Theme default selected' });
  assert.equal(page.cssVar('--font-family'), '');
});

test('choosing Monospace explicitly is applied and saved, not dropped as the default', () => {
  const select = document.getElementById('theme-font-select');
  select.value = 'mono';
  select.dispatchEvent(new Event('change'));
  assert.match(page.cssVar('--font-family'), /Fira Code/);
  assert.equal(page.savedTheme().font, 'mono');
});
