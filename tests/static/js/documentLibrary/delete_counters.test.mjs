// Deleting one document from the Documents library (static/js/documentLibrary.js).
// The header count and the language chips render from the per-language counts
// the library fetched. A delete that only removes the card leaves "3 documents"
// and a "markdown (1)" chip for a document that is gone, until the next full
// refetch (#1809).
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

const now = new Date().toISOString();
const DOCS = [
  { id: 'py-1', title: 'one.py', language: 'python', session_id: 's1', preview: 'a', updated_at: now },
  { id: 'py-2', title: 'two.py', language: 'python', session_id: 's1', preview: 'b', updated_at: now },
  { id: 'md-1', title: 'notes.md', language: 'markdown', session_id: 's1', preview: 'c', updated_at: now },
];

fake.route('GET', '/api/documents/library', () => ({
  documents: DOCS,
  total: DOCS.length,
  languages: { python: 2, markdown: 1 },
  session_count: 1,
}));
const deleted = [];
fake.route('DELETE', /^\/api\/document\/[^/]+$/, ({ url }) => {
  deleted.push(url.pathname.split('/').pop());
  return { ok: true };
});

document.body.innerHTML = '<div id="toast"></div>';
const { initLibrary, openLibrary } = await import('../../../../static/js/documentLibrary.js');
initLibrary({
  apiBase: '',
  esc: (s) => String(s ?? ''),
  getDocs: () => new Map(),
  isOpen: () => false,
  createDocument() {},
  newDocument() {},
  loadDocument() {},
  switchToDoc() {},
  openPanel() {},
  addDocToTabs() {},
  syncDocIndicator() {},
});

function chips() {
  return [...document.querySelectorAll('#doclib-chips .memory-cat-chip')].map((c) => c.textContent);
}

test('deleting the only markdown document updates the count and drops its chip', async () => {
  openLibrary();
  await waitFor(() => document.querySelectorAll('#doclib-grid .doclib-card').length === 3, { what: 'three cards' });
  assert.equal(document.getElementById('doclib-stats').textContent, '3 documents');
  assert.deepEqual(chips(), ['all (3)', 'python (2)', 'markdown (1)']);

  const card = [...document.querySelectorAll('#doclib-grid .doclib-card')]
    .find((c) => c.textContent.includes('notes.md'));
  const deleteBtn = [...card.querySelectorAll('.doclib-card-expanded-actions button')]
    .find((b) => b.textContent.trim() === 'Delete');
  deleteBtn.click();
  await waitFor(() => document.getElementById('styled-confirm-ok'), { what: 'the delete confirmation' });
  document.getElementById('styled-confirm-ok').click();
  await waitFor(() => deleted.length === 1, { what: 'the DELETE request' });
  await waitFor(() => document.getElementById('doclib-stats').textContent !== '3 documents', {
    what: 'the header count to change',
  });

  assert.deepEqual(deleted, ['md-1']);
  assert.equal(document.getElementById('doclib-stats').textContent, '2 documents');
  assert.deepEqual(chips(), ['all (2)', 'python (2)']);
});
