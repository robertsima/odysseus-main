// URL-bearing and style attributes in the allowed-HTML sanitizer of
// static/js/markdown.js. A link or image that survives with a javascript:,
// vbscript: or data: URL runs script when the user clicks or the browser
// loads it, so the check has to see through the spellings browsers accept:
// mixed case, leading spaces, control characters inside the scheme, and the
// second candidate of a srcset.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';

const dom = installDom();
after(() => dom.restore());
const { sanitizeAllowedHtml } = await import('../../../../static/js/markdown.js');

function clean(html) {
  const tpl = document.createElement('template');
  tpl.innerHTML = sanitizeAllowedHtml(html);
  return tpl.content;
}

test('drops script URLs however the scheme is spelled', () => {
  const spellings = [
    'javascript:alert(1)',
    '  JavaScript:alert(1)',
    'java&#9;script:alert(1)',
    'java&#x0A;script:alert(1)',
    'vbscript:msgbox(1)',
    'data:text/html,<script>alert(1)</script>',
  ];
  for (const url of spellings) {
    const link = clean(`<a href="${url}">x</a>`).querySelector('a');
    assert.equal(link.getAttribute('href'), null, `href="${url}" was kept`);
  }
});

test('drops a srcset whose later candidate is a script URL', () => {
  const img = clean('<img src="ok.png" srcset="ok.png 1x, javascript:alert(1) 2x">').querySelector('img');

  assert.equal(img.getAttribute('srcset'), null);
  assert.equal(img.getAttribute('src'), 'ok.png');
});

test('drops inline styles that can run script and keeps plain ones', () => {
  const out = clean(
    '<span id="url" style="background:url(java&#9;script:alert(1))">a</span>' +
    '<span id="expr" style="width: expression(alert(1))">b</span>' +
    '<span id="plain" style="color: red">c</span>');

  assert.equal(out.querySelector('#url').getAttribute('style'), null);
  assert.equal(out.querySelector('#expr').getAttribute('style'), null);
  assert.equal(out.querySelector('#plain').getAttribute('style'), 'color: red');
});

test('keeps ordinary links and image sources', () => {
  const out = clean('<a href="https://example.com/a?b=c">x</a><img srcset="a.png 1x, b.png 2x">');

  assert.equal(out.querySelector('a').getAttribute('href'), 'https://example.com/a?b=c');
  assert.equal(out.querySelector('img').getAttribute('srcset'), 'a.png 1x, b.png 2x');
});
