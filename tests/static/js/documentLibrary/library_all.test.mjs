// The Library modal (static/js/documentLibrary.js) opens on All, keeps Chats,
// Research and Archived behind "More", and still works against a server
// without /api/library/*.
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

// happy-dom has no canvas; the loading spinner draws on one.
const noopCtx = new Proxy(function () {}, { get: () => () => noopCtx, set: () => true });
window.HTMLCanvasElement.prototype.getContext = () => noopCtx;

let libraryItems = () => ({
  items: [{
    id: 'note:n1', kind: 'note', title: 'Call notes', type: 'note', breadcrumb: [],
    updated_at: '2026-10-21T09:00:00Z', actions: ['open'],
  }],
  total: 1, projects: [], vault_shared: true,
});
fake.route('GET', '/api/library/items', () => libraryItems());
fake.route('GET', '/api/library/projects', () => ({ projects: [] }));
fake.route('GET', '/api/documents/library', () => ({
  documents: [{ id: 'd1', title: 'Old doc', language: 'markdown', session_id: null, preview: 'x', version_count: 1, updated_at: new Date().toISOString() }],
  total: 1, languages: { markdown: 1 }, session_count: 0,
}));
fake.route('GET', /^\/api\/(sessions|history|chats)/, () => ({ sessions: [] }));

const { initLibrary, openLibrary, closeLibrary } = await import('../../../../static/js/documentLibrary.js');
initLibrary({
  apiBase: '', esc: (s) => String(s ?? ''), getDocs: () => new Map(), isOpen: () => false,
  createDocument() {}, newDocument() {}, loadDocument() {}, switchToDoc() {}, openPanel() {},
  addDocToTabs() {}, syncDocIndicator() {},
});

const shown = (panel) => document.querySelector(`[data-doclib-panel="${panel}"]`).style.display !== 'none';
const reopen = (opts) => {
  closeLibrary();
  document.getElementById('doclib-modal')?.remove();
  openLibrary(opts);
};

test('the Library opens on All with the five filters and a More menu', async () => {
  reopen();
  await waitFor(() => document.querySelector('.lib-card'), { what: 'the All view cards' });
  assert.deepEqual([...document.querySelectorAll('[data-lib-filter]')].map((b) => b.textContent.trim()),
    ['All', 'Notes', 'Documents', 'Files', 'Projects']);
  assert.equal(document.querySelector('.lib-filter.active').dataset.libFilter, 'all');
  assert.equal(shown('all'), true);
  assert.equal(shown('documents'), false);
  assert.deepEqual([...document.querySelectorAll('#doclib-more-menu [data-doclib-tab]')].map((b) => b.textContent.trim()),
    ['Chats', 'Research', 'Archived']);
});

test("the Documents filter shows today's Documents panel with its controls", async () => {
  reopen();
  await waitFor(() => document.querySelector('.lib-card'), { what: 'cards' });
  document.querySelector('[data-lib-filter="documents"]').click();
  assert.equal(shown('documents'), true);
  assert.equal(shown('all'), false);
  assert.ok(document.getElementById('doclib-import-file-btn'));
  assert.ok(document.getElementById('doclib-create-btn'));
  assert.equal(document.querySelector('.lib-filter.active').dataset.libFilter, 'documents');
});

test('openLibrary({tab: "chats"}) still opens Chats, and Back returns to All', async () => {
  reopen({ tab: 'chats' });
  assert.equal(shown('chats'), true);
  assert.equal(shown('all'), false);
  assert.equal(document.getElementById('doclib-filters').hidden, true);
  assert.equal(document.getElementById('doclib-back').hidden, false);
  document.getElementById('doclib-back').click();
  assert.equal(shown('all'), true);
  assert.equal(document.getElementById('doclib-back').hidden, true);
});

test('a More item switches to its view', async () => {
  reopen();
  document.getElementById('doclib-more-btn').click();
  assert.equal(document.getElementById('doclib-more-menu').hidden, false);
  document.querySelector('#doclib-more-menu [data-doclib-tab="archive"]').click();
  assert.equal(shown('archive'), true);
  assert.equal(document.getElementById('doclib-more-menu').hidden, true);
});

test('against an older server (404) the Library falls back to the Documents view', async () => {
  libraryItems = () => new Response('{"detail":"Not Found"}', { status: 404, headers: { 'Content-Type': 'application/json' } });
  reopen();
  await waitFor(() => shown('documents'), { what: 'the fallback to Documents' });
  assert.equal(shown('all'), false);
  assert.equal(document.getElementById('doclib-filters').hidden, true);
  await waitFor(() => document.querySelector('#doclib-grid .doclib-card'), { what: 'the document card' });
  assert.ok(!document.querySelector('.lib-card'), 'no half-rendered All cards');
});
