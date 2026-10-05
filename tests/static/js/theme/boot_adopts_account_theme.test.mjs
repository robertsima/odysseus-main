// A browser that already has a theme still takes a newer one from the
// account, and applies all of it: colors, font, density, background pattern,
// frosted glass and text size. Older builds asked the server only when this
// browser had no theme at all, and then applied the five base colors only.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { waitFor } from '../_support/dom.mjs';
import { bootTheme, THEME_KEY } from './_account.mjs';

const ACCOUNT_THEME = {
  name: 'ume',
  colors: { bg: '#fff0f5', fg: '#d4608a', panel: '#fff8fa', border: '#f0c0d0', red: '#ff6b9d' },
  font: 'serif',
  density: 'compact',
  bgPattern: 'dots',
  frosted: true,
  uiScale: '125',
};

const page = bootTheme({
  local: {
    [THEME_KEY]: {
      name: 'dark',
      colors: { bg: '#101010', fg: '#e0e0e0', panel: '#181818', border: '#303030', red: '#ff5555' },
      updated_at: 100,
    },
  },
  account: { theme: { value: ACCOUNT_THEME, updated_at: 900 } },
});
after(() => page.restore());

await import('../../../../static/js/theme.js');

test('a newer account theme replaces the one this browser had', async () => {
  await waitFor(() => page.cssVar('--bg') === '#fff0f5', { what: 'the account theme to paint' });
  const saved = page.savedTheme();
  assert.equal(saved.name, 'ume');
  assert.equal(saved.updated_at, 900);
});

test('every part of the adopted theme is applied, not only its colors', async () => {
  await waitFor(() => page.cssVar('--bg') === '#fff0f5', { what: 'the account theme to paint' });
  const html = document.documentElement;
  assert.equal(page.cssVar('--fg'), '#d4608a');
  assert.match(page.cssVar('--font-family'), /Georgia/);
  assert.ok(html.classList.contains('density-compact'), 'density');
  assert.ok(document.body.classList.contains('bg-pattern-dots'), 'background pattern');
  assert.ok(document.body.classList.contains('theme-frosted'), 'frosted glass');
  assert.ok(html.classList.contains('ui-scale-125'), 'text size');
});
