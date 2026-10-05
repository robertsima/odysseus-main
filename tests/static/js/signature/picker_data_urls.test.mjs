// The signature picker renders each saved signature's data URL into an
// <img src>. The URL comes from the API, so a stored value that is an SVG (which
// can carry script), a javascript: URL, or a string that closes the attribute
// must never reach the page or come back as the picked signature. Only PNG
// data URLs, which the drawing pad produces, are shown and returned.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom();
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';
fake.route('GET', '/api/signatures', () => ({
  signatures: [
    { id: 'png', data_url: PNG, name: 'Mine', width: 1, height: 1 },
    { id: 'svg', data_url: 'data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9YWxlcnQoMSk+PC9zdmc+', name: 'SVG' },
    { id: 'quote', data_url: `${PNG}" onerror="window.__pwned = 1`, name: 'Quote' },
    { id: 'js', data_url: 'javascript:window.__pwned = 1', name: 'JS' },
  ],
}));

const { pick } = await import('../../../../static/js/signature.js');

test('only PNG data URLs are rendered and can be picked', async () => {
  const picked = pick();
  await waitFor(() => document.querySelector('.sig-modal-overlay'), { what: 'the picker' });

  const tiles = [...document.querySelectorAll('.sig-tile')];
  assert.deepEqual(tiles.map((t) => t.dataset.id), ['png']);
  const images = [...document.querySelectorAll('.sig-modal-overlay img')];
  assert.deepEqual(images.map((img) => img.getAttribute('src')), [PNG]);
  assert.ok(images.every((img) => !img.hasAttribute('onerror')));

  tiles[0].click();
  const result = await picked;
  assert.equal(result.id, 'png');
  assert.equal(result.dataUrl, PNG);
});
