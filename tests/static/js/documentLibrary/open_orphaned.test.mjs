// Opening a document from the Documents library (static/js/documentLibrary.js)
// when the document has no session. Closing an AI-written document detaches it
// from its chat (session_id becomes null). Both Open controls on its card, the
// expanded card's Open button and the card menu's Open item, must still open
// it in the editor by id (#1602). A control that waits for a session does
// nothing for these documents.
import assert from 'node:assert/strict';
import { after, beforeEach, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom({ width: 1440 });
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

const ORPHAN = {
  id: 'doc-orphan',
  title: 'Closed draft',
  language: 'markdown',
  session_id: null,
  session_name: '',
  preview: 'Body of the closed draft',
  version_count: 2,
  updated_at: new Date().toISOString(),
};

fake.route('GET', '/api/documents/library', () => ({
  documents: [ORPHAN],
  total: 1,
  languages: { markdown: 1 },
  session_count: 0,
}));

const loaded = [];
const { initLibrary, openLibrary, closeLibrary } = await import('../../../../static/js/documentLibrary.js');
initLibrary({
  apiBase: '',
  esc: (s) => String(s ?? ''),
  getDocs: () => new Map(),
  isOpen: () => false,
  createDocument() {},
  newDocument() {},
  loadDocument: (id) => loaded.push(id),
  switchToDoc() {},
  openPanel() {},
  addDocToTabs() {},
  syncDocIndicator() {},
});

async function openOrphanCard() {
  closeLibrary();
  document.getElementById('doclib-modal')?.remove();
  openLibrary();
  await waitFor(() => document.querySelector('#doclib-grid .doclib-card'), { what: 'the document card' });
  return document.querySelector('#doclib-grid .doclib-card');
}

// The control is named by its label: "Open" is what the person clicks.
function openControl(container) {
  return [...container.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Open');
}

beforeEach(() => {
  loaded.length = 0;
});

test('the expanded card Open button opens a session-less document in the editor', async () => {
  const card = await openOrphanCard();
  const openBtn = openControl(card.querySelector('.doclib-card-expanded-actions'));
  assert.ok(openBtn, 'the card has an Open button');
  assert.equal(openBtn.disabled, false);

  openBtn.click();
  await waitFor(() => loaded.length > 0, { what: 'the editor to load the document' });

  assert.deepEqual(loaded, [ORPHAN.id]);
});

test('the card menu Open item opens a session-less document in the editor', async () => {
  const card = await openOrphanCard();
  const openItem = openControl(card.querySelector('.doclib-card-dropdown'));
  assert.ok(openItem, 'the card menu has an Open item');
  assert.equal(openItem.disabled, false);

  openItem.click();
  await waitFor(() => loaded.length > 0, { what: 'the editor to load the document' });

  assert.deepEqual(loaded, [ORPHAN.id]);
});
