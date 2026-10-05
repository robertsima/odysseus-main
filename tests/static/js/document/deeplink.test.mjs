// Document deep links in static/js/document.js. A chat links a document as
// `#document-<id>`. Opening the app on such a URL (a refresh, a pasted link)
// and changing the hash in the URL bar must both open that document in the
// editor; a link to a document that no longer exists must say so instead of
// doing nothing (#560).
import assert from 'node:assert/strict';
import { after, test } from 'node:test';

import { installDom, waitFor } from '../_support/dom.mjs';
import { installFetchFake } from '../_support/fetchFake.mjs';

const dom = installDom({ url: 'http://localhost/#document-doc-on-load', width: 1440 });
const fake = installFetchFake();
after(async () => {
  fake.restore();
  await dom.restore();
});

function storedDoc(id) {
  return {
    id,
    session_id: null,
    title: `Doc ${id}`,
    language: 'markdown',
    current_content: `Body of ${id}`,
    version_count: 1,
  };
}
fake.route('GET', '/api/document/doc-on-load', () => storedDoc('doc-on-load'));
fake.route('GET', '/api/document/doc-from-hash', () => storedDoc('doc-from-hash'));
fake.route('GET', '/api/document/doc-gone', () => new Response(JSON.stringify({ detail: 'Not found' }), {
  status: 404,
  headers: { 'Content-Type': 'application/json' },
}));

document.body.innerHTML = '<div id="toast"></div>';
const documentModule = await import('../../../../static/js/document.js');
documentModule.init('');

test('a document link in the URL at load opens that document', async () => {
  await waitFor(() => documentModule.getCurrentDocId() === 'doc-on-load', { what: 'the linked document to open' });
});

test('changing the hash to a document link opens that document', async () => {
  window.location.hash = '#document-doc-from-hash';
  await waitFor(() => documentModule.getCurrentDocId() === 'doc-from-hash', { what: 'the linked document to open' });
});

test('a link to a missing document shows an error', async () => {
  const toast = document.getElementById('toast');
  assert.equal(toast.classList.contains('error'), false);

  window.location.hash = '#document-doc-gone';
  await waitFor(() => toast.classList.contains('error'), { what: 'the error toast' });

  assert.ok(toast.classList.contains('show'));
  assert.notEqual(toast.textContent.trim(), '');
  assert.equal(documentModule.getCurrentDocId(), 'doc-from-hash', 'the open document stays open');
});
