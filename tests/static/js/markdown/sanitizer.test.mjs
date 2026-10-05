// Runs the real allowed-HTML sanitizer from static/js/markdown.js in a DOM.
// Assistant and document text can carry raw HTML; anything the sanitizer
// leaves as an inline event handler runs in the user's session (XSS).
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';

const dom = installDom();
after(() => dom.restore());
const { sanitizeAllowedHtml } = await import('../../../../static/js/markdown.js');

function parse(html) {
  const tpl = document.createElement('template');
  tpl.innerHTML = html;
  return tpl.content;
}

const UNTRUSTED =
  '<img src="cat.png" alt="a cat" onerror="alert(1)">' +
  '<a href="/docs" title="docs" onclick="steal()" ONMOUSEOVER="steal()">docs</a>';

test('strips onerror and onclick event handlers from kept elements', () => {
  const out = parse(sanitizeAllowedHtml(UNTRUSTED));

  const handlers = [...out.querySelectorAll('*')].flatMap((el) =>
    [...el.attributes].filter((attr) => attr.name.toLowerCase().startsWith('on')).map((attr) => `${el.tagName}[${attr.name}]`));
  assert.deepEqual(handlers, []);
  assert.ok(out.querySelector('img'), 'the image itself is kept');
  assert.ok(out.querySelector('a'), 'the link itself is kept');
});

test('keeps the harmless attributes of an element it cleans', () => {
  const out = parse(sanitizeAllowedHtml(UNTRUSTED));

  const img = out.querySelector('img');
  const link = out.querySelector('a');
  assert.equal(img.getAttribute('src'), 'cat.png');
  assert.equal(img.getAttribute('alt'), 'a cat');
  assert.equal(link.getAttribute('href'), '/docs');
  assert.equal(link.getAttribute('title'), 'docs');
});
