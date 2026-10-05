// Notes are written by people and by the model (an agent can save a note), so
// a note's image URL and text are untrusted. The Notes panel must not render
// a script-capable image (an SVG data: URL can run script) or turn note text
// into markup.
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

const SVG_DATA_URL = 'data:image/svg+xml;base64,' + Buffer.from(
  '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>',
).toString('base64');
const PNG_DATA_URL = 'data:image/png;base64,iVBORw0KGgo=';

const NOTES = [
  { id: 'svg', title: 'svg image', content: '', image_url: SVG_DATA_URL },
  { id: 'js', title: 'js image', content: '', image_url: 'javascript:alert(1)' },
  { id: 'png', title: 'png image', content: '', image_url: PNG_DATA_URL },
  {
    id: 'text',
    title: 'text',
    content: '<img src=x onerror="alert(1)"> see https://example.test/a"onmouseover="alert(1) now',
  },
];

fake.route('GET', '/api/notes', () => ({ notes: NOTES }));

document.body.innerHTML = '<div id="chat-container"></div>';
const notes = await import('../../../../static/js/notes.js');

notes.openPanel();
await waitFor(() => document.querySelectorAll('.note-card').length === NOTES.length, {
  what: 'the note cards to render',
});

function card(id) {
  return document.querySelector(`.note-card[data-note-id="${id}"]`);
}

// Compare strings, not elements: inspecting a DOM node for a failure message
// never finishes.
function imageSrc(id) {
  return card(id).querySelector('img')?.getAttribute('src') ?? null;
}

test('a note image that is an SVG data URL is not rendered', () => {
  assert.equal(imageSrc('svg'), null);
});

test('a note image with a javascript: URL is not rendered', () => {
  assert.equal(imageSrc('js'), null);
});

test('a raster data URL still renders as the note image', () => {
  assert.equal(imageSrc('png'), PNG_DATA_URL);
});

test('note text renders as text, and its links cannot add attributes', () => {
  const preview = card('text').querySelector('.note-content-preview');
  assert.equal(preview.querySelectorAll('img').length, 0, 'markup in the note text became an element');
  assert.match(preview.textContent, /<img src=x onerror="alert\(1\)">/);
  const links = [...preview.querySelectorAll('a')];
  assert.equal(links.length, 1);
  assert.ok(links[0].getAttribute('href').startsWith('https://example.test/a'));
  // The quote in the URL stays inside the href value.
  assert.deepEqual(
    links[0].getAttributeNames().filter((name) => name.startsWith('on')),
    ['onclick'],
  );
});

test('editing a note with an SVG image shows no image in the edit form', async () => {
  card('svg').querySelector('.note-card-title').click();
  await waitFor(() => document.querySelector('.note-form'), { what: 'the edit form' });
  assert.equal(document.querySelector('.note-form img')?.getAttribute('src') ?? null, null);
});
