// New font/effect choices must survive account restoration and switching effects.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { waitFor } from '../_support/dom.mjs';
import { bootTheme } from './_account.mjs';
const page = bootTheme({ account: { theme: { value: {
  name: 'dark', colors: { bg:'#111417', fg:'#f7f8fa', panel:'#1b2127', border:'#59636d', red:'#d7b35a' },
  font: 'editorial', bgPattern: 'rings', bgEffectIntensity: 0.4, bgEffectSize: 1.5,
}, updated_at: 900 } } });
after(() => page.restore());
const theme = await import('../../../../static/js/theme.js');
test('expanded font and effect restore from account without a monospace fallback', async () => {
  await waitFor(() => document.body.classList.contains('bg-pattern-rings'));
  assert.match(page.cssVar('--font-family'), /Palatino/);
  assert.equal(page.savedTheme().font, 'editorial');
  assert.equal(page.savedTheme().bgPattern, 'rings');
  assert.equal(page.cssVar('--bg-effect-intensity'), '0.4');
  assert.equal(page.cssVar('--bg-effect-size'), '1.5');
});
test('switching every new effect off removes its class', () => {
  for (const effect of ['grid', 'diagonal', 'rings', 'aurora']) {
    theme.applyBgPattern(effect);
    theme.applyBgPattern('none');
    assert.equal(document.body.classList.contains('bg-pattern-' + effect), false);
  }
});
