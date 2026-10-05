// The line-number gutter beside the document editor (static/js/document.js).
// It numbers every line of the open document, and it follows the textarea as
// that scrolls. The gutter itself is overflow-hidden, so it cannot scroll;
// its inner column is moved instead (#1501, #1496). Row heights for wrapped
// lines depend on layout and are left to the browser tests.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom({ width: 1440 });
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

document.body.innerHTML = '<div id="chat-container"></div><div id="toast"></div>';
const documentModule = await import('../../../../static/js/document.js');
documentModule.init('');
documentModule.openPanel();

const labels = () => [...document.querySelectorAll('#doc-line-numbers .doc-line-number-label')].map((l) => l.textContent);

test('every line of the open document is numbered', () => {
  documentModule.handleDocUpdate({
    doc_id: 'n1', action: 'create', title: 'Notes', language: 'markdown', content: 'one\ntwo\nthree\nfour', version: 1,
  });
  assert.deepEqual(labels(), ['1', '2', '3', '4']);
});

test('the gutter follows the textarea as it scrolls', async () => {
  const lines = Array.from({ length: 60 }, (_, i) => `line ${i + 1}`).join('\n');
  documentModule.handleDocUpdate({ doc_id: 'n2', action: 'create', title: 'Long', language: 'markdown', content: lines, version: 1 });
  const textarea = document.getElementById('doc-editor-textarea');
  const column = document.querySelector('#doc-line-numbers .doc-line-number-content');
  assert.ok(column, 'the gutter has an inner column to move');

  textarea.scrollTop = 120;
  textarea.dispatchEvent(new Event('scroll'));

  await waitFor(() => column.style.transform === 'translateY(-120px)', { what: 'the gutter column to follow the scroll' });
  assert.equal(document.getElementById('doc-line-numbers').scrollTop, 0);
});
