// The Library > Research inline preview (static/js/documentLibrary.js) lists a
// report's sources from the research detail the API returns. Those URLs come
// from web pages the research agent read, so they are untrusted: a link is
// drawn only for an http(s) URL, never for javascript: or data:. A failed
// detail load shows its message as text, not markup.
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

const REPORT = { id: 'r1', query: 'solar panels', status: 'done', source_count: 3 };
let detail = () => ({
  sources: [
    { title: 'Good source', url: 'https://example.com/a?x=1&y=2' },
    { title: 'Script source', url: 'javascript:alert(document.cookie)' },
    { title: 'Data source', url: 'data:text/html,<script>alert(1)</script>' },
  ],
  summary: 'Panels are cheap.',
});
fake.route('GET', '/api/research/library', () => ({ research: [REPORT] }));
fake.route('GET', '/api/research/detail/r1', () => detail());
fake.route('GET', '/api/documents/library', () => ({ documents: [], total: 0, languages: {}, session_count: 0 }));
fake.route('GET', /^\/api\/(sessions|history)/, () => ({ sessions: [] }));

const { initLibrary, openLibrary } = await import('../../../../static/js/documentLibrary.js');
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
initLibrary({
  apiBase: '', esc, getDocs: () => [], isOpen: () => false, createDocument() {}, newDocument() {},
  loadDocument() {}, switchToDoc() {}, openPanel() {}, addDocToTabs() {}, syncDocIndicator() {},
});
openLibrary();
document.querySelector('[data-doclib-tab="research"]').click();

async function openPreview() {
  await waitFor(() => document.querySelector('#doclib-research-grid .doclib-chat-row'), { what: 'the research row' });
  document.querySelector('#doclib-research-grid .doclib-chat-row').click();
  await waitFor(() => !document.querySelector('.doclib-chat-preview').textContent.startsWith('Loading')
    && document.querySelector('.doclib-chat-preview').textContent.length > 0, { what: 'the preview' });
  return document.querySelector('.doclib-chat-preview');
}

test('only http(s) sources become links', async () => {
  const preview = await openPreview();

  const links = [...preview.querySelectorAll('.doclib-research-sources a')];
  assert.deepEqual(links.map((a) => a.getAttribute('href')), ['https://example.com/a?x=1&y=2']);
  assert.ok(preview.textContent.includes('Script source'), 'the unsafe source is still listed, as text');
  assert.ok(preview.textContent.includes('Data source'));
});
