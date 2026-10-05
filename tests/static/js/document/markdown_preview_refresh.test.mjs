// AI updates while the Markdown preview is showing (static/js/document.js).
// The preview is a rendered copy of the editor text. When an AI update lands
// while it is visible, the preview must show the new text: animating the
// hidden editor instead leaves the old text on screen (#2182). A large edit
// opens the diff review, which lives in the editor, so the preview steps
// aside for it.
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom({ width: 1440 });
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});
fake.route('PUT', /^\/api\/document\/[^/]+$/, () => ({ version_count: 2 }));

document.body.innerHTML = '<div id="chat-container"></div><div id="toast"></div>';
const documentModule = await import('../../../../static/js/document.js');
documentModule.init('');
documentModule.openPanel();

const BODY = 'first line\nsecond line\nthird line\nfourth line';

const preview = () => document.getElementById('doc-md-preview');
const previewVisible = () => preview().style.display !== 'none';

// The Write/Preview switch in the editor header. The click carries
// coordinates away from the origin, where document.js would read it as a
// click on the hidden email Send button.
function showPreview() {
  const opt = document.querySelector('#doc-md-view-toggle .md-view-opt[data-mdview="preview"]');
  opt.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, clientX: 400, clientY: 200 }));
  assert.ok(previewVisible(), 'the preview is showing');
}

function openMarkdownDoc(id, content = BODY) {
  documentModule.handleDocUpdate({ doc_id: id, action: 'create', title: `Doc ${id}`, language: 'markdown', content, version: 1 });
}

test('a small AI edit re-renders the visible preview', () => {
  openMarkdownDoc('small-edit');
  showPreview();

  documentModule.handleDocUpdate({
    doc_id: 'small-edit', action: 'edit', title: 'Doc small-edit', language: 'markdown',
    content: BODY.replace('second line', 'second line, revised'), version: 2,
  });

  assert.ok(previewVisible());
  assert.match(preview().textContent, /second line, revised/);
});

test('a large AI edit leaves the preview for the diff review', () => {
  openMarkdownDoc('large-edit');
  showPreview();

  documentModule.handleDocUpdate({
    doc_id: 'large-edit', action: 'edit', title: 'Doc large-edit', language: 'markdown',
    content: 'one\ntwo\nthree\nfour', version: 2,
  });

  assert.equal(previewVisible(), false);
  assert.ok(document.getElementById('doc-editor-wrap').classList.contains('diff-mode'));
});

test('an update that switches to another document shows that document in the preview', () => {
  openMarkdownDoc('updated-next', 'old text of the second document');
  openMarkdownDoc('shown-first', 'text of the first document');
  showPreview();
  assert.match(preview().textContent, /text of the first document/);

  documentModule.handleDocUpdate({
    doc_id: 'updated-next', action: 'update', title: 'Doc updated-next', language: 'markdown',
    content: 'new text of the second document', version: 2,
  });

  assert.equal(documentModule.getCurrentDocId(), 'updated-next');
  assert.ok(previewVisible());
  assert.match(preview().textContent, /new text of the second document/);
});
