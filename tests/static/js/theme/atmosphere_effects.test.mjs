import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { bootTheme, settled } from './_account.mjs';

const page = bootTheme();
after(() => page.restore());
// Canvas rendering and the frame clock are browser boundaries. Keep the real
// selector, theme application and persistence code under test.
const frames = new Map();
let nextFrame = 0;
globalThis.requestAnimationFrame = fn => { frames.set(++nextFrame, fn); return nextFrame; };
globalThis.cancelAnimationFrame = id => frames.delete(id);
const gradient = { addColorStop() {} };
const context = new Proxy({}, { get: (_, key) => key.startsWith('create') ? () => gradient : () => {} });
HTMLCanvasElement.prototype.getContext = () => context;
const theme = await import('../../../../static/js/theme.js');
const effects = ['fog', 'waves', 'fireflies', 'snowfall', 'ripples'];

test('five atmosphere choices render, replace each other, and save all effect controls', async () => {
  await settled();
  theme.initThemeUI();
  frames.clear();
  const selector = document.getElementById('theme-bg-pattern-select');
  for (const effect of effects) {
    selector.value = effect;
    selector.dispatchEvent(new Event('change'));
    const canvas = document.getElementById(`${effect}-canvas`);
    assert.ok(canvas, `${effect} must create a canvas`);
    assert.equal(canvas.getAttribute('aria-hidden'), 'true');
    assert.equal(canvas.style.pointerEvents, 'none');
    assert.equal(document.querySelectorAll('canvas').length, 1);
    assert.equal(frames.size, 1, 'switching must cancel the previous frame');
    assert.equal(page.savedTheme().bgPattern, effect);
  }
  theme.applyBgEffectColor('#88aaff');
  theme.applyBgEffectSize(2);
  theme.applyBgEffectIntensity(0);
  assert.equal(page.cssVar('--bg-effect-color'), '#88aaff');
  assert.equal(page.cssVar('--bg-effect-size'), '2');
  assert.equal(page.cssVar('--bg-effect-intensity'), '0');
  theme.applyBgPattern('none');
  assert.equal(document.querySelectorAll('canvas').length, 0);
  assert.equal(frames.size, 0);
  assert.ok(!effects.some(effect => document.body.classList.contains(`bg-pattern-${effect}`)));
  await settled();
  assert.ok(page.puts.some(put => put.key === 'theme' && put.value.bgPattern === 'ripples'));
});
