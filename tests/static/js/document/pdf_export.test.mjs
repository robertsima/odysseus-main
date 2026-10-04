// "Print as PDF" for a markdown document, in static/js/document.js. KaTeX is
// loaded on first use, so on a page that has not shown math yet mdToHtml()
// leaves each formula as a pending placeholder. The export renders into a
// detached container that the page-level typesetting never sees, so it has to
// typeset that container itself before html2pdf rasterises it, or the PDF
// shows raw formula source.
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

// The page loads the vendored KaTeX by adding a <script> to <head>. Here that
// "load" defines a renderer that marks what it typeset, which is enough to see
// whether typesetting ran; happy-dom itself would fetch the file and fail.
const appendToHead = document.head.appendChild.bind(document.head);
document.head.appendChild = (node) => {
  const url = node.src || node.href || '';
  if (!/\/static\/lib\/katex\//.test(url)) return appendToHead(node);
  if (node.tagName === 'SCRIPT') window.katex = { renderToString: (src) => `<span class="katex">${src}</span>` };
  queueMicrotask(() => node.dispatchEvent(new Event('load')));
  return node;
};

let printed = null;
window.html2pdf = () => ({
  set() { return this; },
  from(element) { printed = element.innerHTML; return this; },
  save() { return this; },
});

// happy-dom lays every element out at (0, 0), and the email composer's send
// caret claims clicks by position, so a click at (0, 0) lands on it. Click
// where nothing else is.
function clickAt(element) {
  element.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, clientX: 300, clientY: 300 }));
}

document.body.innerHTML = '<div id="chat-container"><div id="chat-history"></div></div><div id="toast"></div>';
const documentModule = (await import('../../../../static/js/document.js')).default;

test('the PDF gets typeset math, not pending formula placeholders', async () => {
  assert.equal(window.katex, undefined, 'KaTeX starts unloaded, as on a fresh page');
  documentModule.injectFreshDoc({
    id: 'doc-math', title: 'Areas', language: 'markdown',
    content: 'The area is $x^2$ square metres.',
  });
  await waitFor(() => document.getElementById('doc-footer-export-btn'), { what: 'the document footer' });
  await waitFor(() => document.getElementById('doc-editor-textarea')?.value.includes('$x^2$'), { what: 'the document text' });

  clickAt(document.getElementById('doc-footer-export-btn'));
  const item = [...document.querySelectorAll('#doc-export-menu .doc-overflow-item')]
    .find((b) => b.textContent === 'Print as PDF');
  clickAt(item);
  await waitFor(() => printed !== null, { what: 'html2pdf to get the page' });

  assert.match(printed, /<span class="katex">x\^2<\/span>/);
  assert.doesNotMatch(printed, /ody-math-pending/);
});
