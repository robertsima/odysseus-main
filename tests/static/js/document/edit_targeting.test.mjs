// Where an AI edit lands in the editor (static/js/document.js). A freshly
// created document may take over an open tab with the same title, or an empty
// untitled tab. An edit or update names an existing server document and must
// open in that document's own tab: reusing a same-titled tab once showed one
// note's edit inside another note and made that one the chat's active
// document (2026-09-28).
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

function tabIds() {
  return [...document.querySelectorAll('#doc-tab-bar .doc-tab[data-doc-id]')].map((t) => t.dataset.docId);
}

test('an edit to a document that shares an open tab\'s title opens in its own tab', () => {
  documentModule.handleDocUpdate({ doc_id: 'note-a', action: 'create', title: 'Weekly notes', language: 'markdown', content: 'note A body', version: 1 });

  documentModule.handleDocUpdate({ doc_id: 'note-b', action: 'edit', title: 'Weekly notes', language: 'markdown', content: 'note B edited', version: 3 });

  assert.equal(documentModule.getCurrentDocId(), 'note-b');
  assert.ok(tabIds().includes('note-a'), 'the same-titled note keeps its tab');
  assert.ok(tabIds().includes('note-b'), 'the edited note has its own tab');
  assert.equal(document.getElementById('doc-editor-textarea').value, 'note B edited');
});

test('an edit does not take over an empty untitled tab', () => {
  documentModule.handleDocUpdate({ doc_id: 'blank', action: 'create', title: 'Untitled', language: 'markdown', content: '', version: 1 });

  documentModule.handleDocUpdate({ doc_id: 'note-c', action: 'update', title: 'Roadmap', language: 'markdown', content: 'roadmap body', version: 2 });

  assert.equal(documentModule.getCurrentDocId(), 'note-c');
  assert.ok(tabIds().includes('blank'), 'the untitled tab is left alone');
  assert.ok(tabIds().includes('note-c'));
});
